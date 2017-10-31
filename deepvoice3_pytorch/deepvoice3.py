# coding: utf-8

import torch
from torch import nn
from torch.nn import functional as F
from torch.autograd import Variable
import math
import numpy as np

from fairseq.models.fconv import Embedding, Linear, LinearizedConv1d, ConvTBC
from fairseq.models.fconv import grad_multiply


def build_deepvoice3(n_vocab, embed_dim=256, mel_dim=80, linear_dim=4096, r=5,
                     n_speakers=1, speaker_embed_dim=16, padding_idx=None,
                     dropout=(1 - 0.9)):
    encoder = Encoder(
        n_vocab, embed_dim, padding_idx=padding_idx,
        n_speakers=n_speakers, speaker_embed_dim=speaker_embed_dim,
        dropout=dropout,
        convolutions=((64, 5),) * 7)
    decoder = Decoder(
        embed_dim, in_dim=mel_dim, r=r, padding_idx=padding_idx,
        n_speakers=n_speakers, speaker_embed_dim=speaker_embed_dim,
        dropout=dropout,
        convolutions=((128, 5),) * 5)
    converter = Converter(
        in_dim=mel_dim, out_dim=linear_dim, dropout=dropout,
        convolutions=((256, 5),) * 5)
    model = DeepVoice3(
        encoder, decoder, converter, padding_idx=padding_idx,
        mel_dim=mel_dim, linear_dim=linear_dim,
        n_speakers=n_speakers, speaker_embed_dim=speaker_embed_dim)

    return model


class DeepVoice3(nn.Module):
    def __init__(self, encoder, decoder, converter,
                 mel_dim=80, linear_dim=4096,
                 n_speakers=1, speaker_embed_dim=16, padding_idx=None):
        super(DeepVoice3, self).__init__()
        self.mel_dim = mel_dim
        self.linear_dim = linear_dim

        self.encoder = encoder
        self.decoder = decoder
        self.converter = converter
        self.encoder.num_attention_layers = sum(
            [layer is not None for layer in decoder.attention])

        # Speaker embedding
        if n_speakers > 1:
            self.embed_speakers = Embedding(
                n_speakers, speaker_embed_dim, padding_idx)
        self.n_speakers = n_speakers
        self.speaker_embed_dim = speaker_embed_dim

    def get_trainable_parameters(self):
        ''' Avoid updating the position encoding '''
        # return self.parameters()
        pe_query_param_ids = set(map(id, self.decoder.embed_query_positions.parameters()))
        pe_keys_param_ids = set(map(id, self.decoder.embed_keys_positions.parameters()))
        freezed_param_ids = pe_query_param_ids | pe_keys_param_ids
        return (p for p in self.parameters() if id(p) not in freezed_param_ids)

    def forward(self, text_sequences, mel_targets=None, speaker_ids=None,
                text_positions=None, frame_positions=None, input_lengths=None):
        B = text_sequences.size(0)

        if speaker_ids is not None:
            speaker_embed = self.embed_speakers(speaker_ids)
        else:
            speaker_embed = None

        # (B, T, text_embed_dim)
        encoder_outputs = self.encoder(
            text_sequences, lengths=input_lengths, speaker_embed=speaker_embed)

        # (B, T', mel_dim*r)
        mel_outputs, alignments = self.decoder(
            encoder_outputs, mel_targets,
            text_positions=text_positions, frame_positions=frame_positions,
            speaker_embed=speaker_embed, lengths=input_lengths)

        # Reshape
        # (B, T, mel_dim)
        mel_outputs = mel_outputs.view(B, -1, self.mel_dim)

        # (B, T, linear_dim)
        linear_outputs = self.converter(mel_outputs)

        return mel_outputs, linear_outputs, alignments


class Encoder(nn.Module):
    def __init__(self, n_vocab, embed_dim, n_speakers, speaker_embed_dim,
                 padding_idx=None, convolutions=((64, 5),) * 7, dropout=0.1):
        super(Encoder, self).__init__()
        self.dropout = dropout
        self.num_attention_layers = None

        # Text input embeddings
        self.embed_tokens = Embedding(n_vocab, embed_dim, padding_idx)

        # Speaker embedding
        if n_speakers > 1:
            self.speaker_fc1 = Linear(speaker_embed_dim, embed_dim)
            self.speaker_fc2 = Linear(speaker_embed_dim, embed_dim)
        self.n_speakers = n_speakers

        # Non-causual convolutions
        in_channels = convolutions[0][0]
        self.fc1 = Linear(embed_dim, in_channels, dropout=dropout)
        self.projections = nn.ModuleList()
        self.speaker_projections = nn.ModuleList()
        self.convolutions = nn.ModuleList()
        for (out_channels, kernel_size) in convolutions:
            pad = (kernel_size - 1) // 2
            self.projections.append(Linear(in_channels, out_channels)
                                    if in_channels != out_channels else None)
            self.speaker_projections.append(
                Linear(speaker_embed_dim, out_channels) if n_speakers > 1 else None)
            self.convolutions.append(
                ConvTBC(in_channels, out_channels * 2, kernel_size, padding=pad,
                        dropout=dropout))
            in_channels = out_channels
        self.fc2 = Linear(in_channels, embed_dim)

    def forward(self, text_sequences, lengths=None, speaker_embed=None):
        assert self.n_speakers == 1 or speaker_embed is not None

        # embed text_sequences
        x = self.embed_tokens(text_sequences)
        x = F.dropout(x, p=self.dropout, training=self.training)

        # embed speakers
        if speaker_embed is not None:
            # expand speaker embedding for all time steps
            # (B, N) -> (B, T, N)
            ss = speaker_embed.size()
            speaker_embed = speaker_embed.unsqueeze(1).expand(
                ss[0], x.size(1), ss[-1])
            x += F.softsign(self.speaker_fc1(speaker_embed))

        input_embedding = x

        # project to size of convolution
        x = self.fc1(x)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)
        speaker_embed = speaker_embed.transpose(0, 1) if speaker_embed is not None else None

        # １D conv blocks
        for proj, speaker_proj, conv in zip(
                self.projections, self.speaker_projections, self.convolutions):
            residual = x if proj is None else proj(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
            x = conv(x)
            a, b = x.split(x.size(-1) // 2, dim=-1)
            if speaker_proj is not None:
                a = a + F.softsign(speaker_proj(speaker_embed))
            x = a * F.sigmoid(b)
            x = (x + residual) * math.sqrt(0.5)

        # T x B x C -> B x T x C
        x = x.transpose(1, 0)
        speaker_embed = speaker_embed.transpose(0, 1) if speaker_embed is not None else None

        # project back to size of embedding
        keys = self.fc2(x)
        if speaker_embed is not None:
            keys += F.softsign(self.speaker_fc2(speaker_embed))

        # scale gradients (this only affects backward, not forward)
        # if self.num_attention_layers is not None:
        #     keys = grad_multiply(keys, 1.0 / (2.0 * self.num_attention_layers))

        # add output to input embedding for attention
        values = (keys + input_embedding) * math.sqrt(0.5)

        return keys, values


def get_mask_from_lengths(memory, memory_lengths):
    """Get mask tensor from list of length
    Args:
        memory: (batch, max_time, dim)
        memory_lengths: array like
    """
    mask = memory.data.new(memory.size(0), memory.size(1)).byte().zero_()
    for idx, l in enumerate(memory_lengths):
        mask[idx][:l] = 1
    return ~mask


class AttentionLayer(nn.Module):
    def __init__(self, conv_channels, embed_dim, dropout=0.1):
        super(AttentionLayer, self).__init__()
        # projects from output of convolution to embedding dimension
        self.in_projection = Linear(conv_channels, embed_dim)
        # projects from embedding dimension to convolution size
        self.out_projection = Linear(embed_dim, conv_channels)
        self.dropout = dropout

    def forward(self, query, encoder_out, mask=None):
        keys, values = encoder_out
        residual = query

        # attention
        x = self.in_projection(query)
        x = torch.bmm(x, keys)

        if mask is not None:
            mask = mask.view(query.size(0), 1, -1)
            x.data.masked_fill_(mask, -float("inf"))

        # softmax over last dim
        sz = x.size()
        x = F.softmax(x.view(sz[0] * sz[1], sz[2]), dim=1)
        x = x.view(sz)
        attn_scores = x

        x = F.dropout(x, p=self.dropout, training=self.training)

        x = torch.bmm(x, values)

        # scale attention output
        s = values.size(1)
        x = x * (s * math.sqrt(1.0 / s))

        # project back
        x = (self.out_projection(x) + residual) * math.sqrt(0.5)
        return x, attn_scores


def position_encoding_init(n_position, d_pos_vec):
    ''' Init the sinusoid position encoding table '''

    # keep dim 0 for padding token position encoding zero vector
    position_enc = np.array([
        [pos / np.power(10000, 2 * i / d_pos_vec) for i in range(d_pos_vec)]
        if pos != 0 else np.zeros(d_pos_vec) for pos in range(n_position)])

    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2])  # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2])  # dim 2i+1
    return torch.from_numpy(position_enc).type(torch.FloatTensor)


class Decoder(nn.Module):
    def __init__(self, embed_dim, n_speakers, speaker_embed_dim,
                 in_dim=80, r=5,
                 max_positions=512, padding_idx=None,
                 convolutions=((128, 5),) * 4,
                 attention=True, dropout=0.1):
        super(Decoder, self).__init__()
        self.dropout = dropout
        self.in_dim = in_dim
        self.r = r

        in_channels = in_dim * r
        if isinstance(attention, bool):
            # expand True into [True, True, ...] and do the same with False
            attention = [attention] * len(convolutions)

        # Position encodings for query (decoder states) and keys (encoder states)
        self.embed_query_positions = Embedding(
            max_positions, convolutions[0][0], padding_idx)
        self.embed_query_positions.weight.data = position_encoding_init(
            max_positions, convolutions[0][0])
        self.embed_keys_positions = Embedding(
            max_positions, embed_dim, padding_idx)
        self.embed_keys_positions.weight.data = position_encoding_init(
            max_positions, embed_dim)

        self.fc1 = Linear(in_channels, convolutions[0][0], dropout=dropout)
        in_channels = convolutions[0][0]

        # Causual convolutions
        self.projections = nn.ModuleList()
        self.convolutions = nn.ModuleList()
        self.attention = nn.ModuleList()
        for i, (out_channels, kernel_size) in enumerate(convolutions):
            pad = kernel_size - 1
            self.projections.append(Linear(in_channels, out_channels)
                                    if in_channels != out_channels else None)
            self.convolutions.append(
                LinearizedConv1d(in_channels, out_channels * 2, kernel_size,
                                 padding=pad, dropout=dropout))
            self.attention.append(AttentionLayer(out_channels, embed_dim,
                                                 dropout=dropout)
                                  if attention[i] else None)
            in_channels = out_channels
        self.fc2 = Linear(in_channels, in_dim * r)

        self.relu = nn.ReLU(inplace=True)

        self._is_inference_incremental = False
        self.max_decoder_steps = 200

    def forward(self, encoder_out, inputs=None,
                text_positions=None, frame_positions=None,
                speaker_embed=None, lengths=None):

        if inputs is None:
            assert text_positions is not None
            self._start_incremental_inference()
            outputs = self._incremental_forward(encoder_out, text_positions)
            self._stop_incremental_inference()
            return outputs

        # Grouping multiple frames if necessary
        if inputs.size(-1) == self.in_dim:
            inputs = inputs.view(inputs.size(0), inputs.size(1) // self.r, -1)
        assert inputs.size(-1) == self.in_dim * self.r

        keys, values = encoder_out

        if lengths is not None:
            mask = get_mask_from_lengths(keys, lengths)
        else:
            mask = None

        # position encodings
        if text_positions is not None:
            text_pos_embed = self.embed_keys_positions(text_positions)
            keys += text_pos_embed
        if frame_positions is not None:
            frame_pos_embed = self.embed_query_positions(frame_positions)

        # transpose only once to speed up attention layers
        keys = keys.transpose(1, 2).contiguous()

        x = inputs
        x = F.dropout(x, p=self.dropout, training=self.training)

        # project to size of convolution
        x = self.relu(self.fc1(x))
        # x = self.fc1(x)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        # temporal convolutions
        alignments = []
        for proj, conv, attention in zip(
                self.projections, self.convolutions, self.attention):
            residual = x if proj is None else proj(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
            x = conv(x)
            x = conv.remove_future_timesteps(x)
            a, b = x.split(x.size(-1) // 2, dim=-1)
            x = a * F.sigmoid(b)

            # Feed conv output to attention layer as query
            if attention is not None:
                # (B x T x C)
                x = x.transpose(1, 0)
                x = x if frame_positions is None else x + frame_pos_embed
                x, alignment = attention(x, (keys, values), mask=mask)
                # (T x B x C)
                x = x.transpose(1, 0)
                alignments += [alignment]

            # residual
            x = (x + residual) * math.sqrt(0.5)

        # T x B x C -> B x T x C
        x = x.transpose(1, 0)

        # project to mel-spectorgram
        x = self.fc2(x)

        return x, torch.stack(alignments)

    def incremental_inference(self, beam_size=None):
        """Context manager for incremental inference.
        This provides an optimized forward pass for incremental inference
        (i.e., it predicts one time step at a time). If the input order changes
        between time steps, call model.decoder.reorder_incremental_state to
        update the relevant buffers. To generate a fresh sequence, first call
        model.decoder.start_fresh_sequence.
        Usage:
        ```
        with model.decoder.incremental_inference():
            for step in range(maxlen):
                out = model.decoder(tokens[:, :step], positions[:, :step],
                                    encoder_out)
                probs = F.log_softmax(out[:, -1, :])
        ```
        """
        class IncrementalInference(object):

            def __init__(self, decoder, beam_size):
                self.decoder = decoder
                self.beam_size = beam_size

            def __enter__(self):
                self.decoder._start_incremental_inference(self.beam_size)

            def __exit__(self, *args):
                self.decoder._stop_incremental_inference()

        return IncrementalInference(self, beam_size)

    def _start_incremental_inference(self):
        assert not self._is_inference_incremental, \
            'already performing incremental inference'
        self._is_inference_incremental = True

        # save original forward
        self._orig_forward = self.forward

        # switch to incremental forward
        self.forward = self._incremental_forward

        # start a fresh sequence
        self.start_fresh_sequence()

    def _stop_incremental_inference(self):
        # restore original forward
        self.forward = self._orig_forward

        self._is_inference_incremental = False

    def _incremental_forward(self, encoder_out, text_positions):
        assert self._is_inference_incremental

        keys, values = encoder_out
        B = keys.size(0)

        # position encodings
        text_pos_embed = self.embed_keys_positions(text_positions)
        keys += text_pos_embed

        # transpose only once to speed up attention layers
        keys = keys.transpose(1, 2).contiguous()

        outputs = []
        alignments = []

        t = 0
        initial_input = Variable(
            keys.data.new(B, 1, self.in_dim * self.r).zero_())
        current_input = initial_input
        while True:
            frame_pos = Variable(keys.data.new(B, 1).zero_().add_(t + 1)).long()
            frame_pos_embed = self.embed_query_positions(frame_pos)

            if t > 0:
                current_input = outputs[-1]

            x = F.dropout(current_input, p=self.dropout, training=self.training)

            # project to size of convolution
            x = self.relu(self.fc1(x))
            # x = self.fc1(x)

            # temporal convolutions
            ave_alignment = None
            for proj, conv, attention in zip(
                    self.projections, self.convolutions, self.attention):
                residual = x if proj is None else proj(x)

                x = F.dropout(x, p=self.dropout, training=self.training)
                x = conv.incremental_forward(x)
                a, b = x.split(x.size(-1) // 2, dim=-1)
                x = a * F.sigmoid(b)

                # attention
                if attention is not None:
                    x = x + frame_pos_embed
                    x, alignment = attention(x, (keys, values))
                    if ave_alignment is None:
                        ave_alignment = alignment
                    else:
                        ave_alignment = ave_alignment + ave_alignment

                # residual
                x = (x + residual) * math.sqrt(0.5)

            ave_alignment = ave_alignment.div_(len(self.attention))

            # project to mel
            output = self.fc2(x)

            outputs += [output]
            alignments += [ave_alignment]

            t += 1
            if t > 10 and is_end_of_frames(output):
                break
            elif t > self.max_decoder_steps:
                print("Warning! doesn't seems to be converged")
                break

        # Remove 1-element time axis
        alignments = list(map(lambda x: x.squeeze(1), alignments))
        outputs = list(map(lambda x: x.squeeze(1), outputs))

        # Combine outputs for all time steps
        alignments = torch.stack(alignments).transpose(0, 1)
        outputs = torch.stack(outputs).transpose(0, 1).contiguous()

        return outputs, alignments

    def start_fresh_sequence(self):
        """Clear all state used for incremental generation.
        **For incremental inference only**
        This should be called before generating a fresh sequence.
        beam_size is required if using BeamableMM.
        """
        if self._is_inference_incremental:
            self.prev_state = None
            for conv in self.convolutions:
                conv.clear_buffer()


def is_end_of_frames(output, eps=0.2):
    return (output.data <= eps).all()


class Converter(nn.Module):
    def __init__(self, in_dim, out_dim, convolutions=((256, 5),) * 4, dropout=0.1):
        super(Converter, self).__init__()
        self.dropout = dropout
        self.in_dim = in_dim
        self.out_dim = out_dim

        # Non-causual convolutions
        in_channels = convolutions[0][0]
        self.fc1 = Linear(in_dim, in_channels)
        self.projections = nn.ModuleList()
        self.convolutions = nn.ModuleList()
        for (out_channels, kernel_size) in convolutions:
            pad = (kernel_size - 1) // 2
            self.projections.append(Linear(in_channels, out_channels)
                                    if in_channels != out_channels else None)
            self.convolutions.append(
                ConvTBC(in_channels, out_channels * 2, kernel_size, padding=pad,
                        dropout=dropout))
            in_channels = out_channels
        self.fc2 = Linear(in_channels, out_dim)

    def forward(self, mel_outputs):
        x = mel_outputs
        # x = F.dropout(x, p=self.dropout, training=self.training)

        # project to size of convolution
        x = self.fc1(x)

        # B x T x C -> T x B x C
        x = x.transpose(0, 1)

        # １D conv blocks
        for proj, conv in zip(self.projections, self.convolutions):
            residual = x if proj is None else proj(x)
            x = F.dropout(x, p=self.dropout, training=self.training)
            x = conv(x)
            a, b = x.split(x.size(-1) // 2, dim=-1)
            x = a * F.sigmoid(b)
            x = (x + residual) * math.sqrt(0.5)

        # T x B x C -> B x T x C
        x = x.transpose(1, 0)

        return self.fc2(x)