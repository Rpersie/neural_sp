#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Nested attention-based sequence-to-sequence model (pytorch)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import random
import numpy as np
import copy

import torch
import torch.nn.functional as F

from models.pytorch.attention.attention_seq2seq import AttentionSeq2seq
from models.pytorch.linear import LinearND, Embedding, Embedding_LS
from models.pytorch.encoders.load_encoder import load
from models.pytorch.attention.rnn_decoder import RNNDecoder
from models.pytorch.attention.attention_layer import AttentionMechanism
from models.pytorch.criterion import cross_entropy_label_smoothing
from models.pytorch.ctc.decoders.greedy_decoder import GreedyDecoder
from models.pytorch.ctc.decoders.beam_search_decoder import BeamSearchDecoder

LOG_1 = 0


class NestedAttentionSeq2seq(AttentionSeq2seq):

    def __init__(self,
                 input_size,
                 encoder_type,
                 encoder_bidirectional,
                 encoder_num_units,
                 encoder_num_proj,
                 encoder_num_layers,
                 encoder_num_layers_sub,  # ***
                 attention_type,
                 attention_dim,
                 decoder_type,
                 decoder_num_units,
                 decoder_num_layers,
                 decoder_num_units_sub,  # ***
                 decoder_num_layers_sub,  # ***
                 embedding_dim,
                 embedding_dim_sub,  # ***
                 dropout_input,
                 dropout_encoder,
                 dropout_decoder,
                 dropout_embedding,
                 main_loss_weight,  # ***
                 sub_loss_weight,  # ***
                 num_classes,
                 num_classes_sub,  # ***
                 parameter_init_distribution='uniform',
                 parameter_init=0.1,
                 recurrent_weight_orthogonal=False,
                 init_forget_gate_bias_with_one=True,
                 subsample_list=[],
                 subsample_type='drop',
                 bridge_layer=False,
                 init_dec_state='first',
                 sharpening_factor=1,  # TODO: change arg name
                 logits_temperature=1,
                 sigmoid_smoothing=False,
                 coverage_weight=0,
                 ctc_loss_weight_sub=0,  # ***
                 attention_conv_num_channels=10,
                 attention_conv_width=201,
                 num_stack=1,
                 splice=1,
                 conv_channels=[],
                 conv_kernel_sizes=[],
                 conv_strides=[],
                 poolings=[],
                 activation='relu',
                 batch_norm=False,
                 scheduled_sampling_prob=0,
                 scheduled_sampling_ramp_max_step=0,
                 label_smoothing_prob=0,
                 weight_noise_std=0,
                 encoder_residual=False,
                 encoder_dense_residual=False,
                 decoder_residual=False,
                 decoder_dense_residual=False,
                 decoding_order='attend_generate_update',
                 bottleneck_dim=256,
                 bottleneck_dim_sub=256,  # ***
                 backward_sub=False,  # ***
                 num_heads=1,
                 num_heads_sub=1,  # ***
                 num_heads_dec_out_sub=1,  # ***
                 usage_dec_sub='update_decoder',  # or all or no_use
                 gating_mechanism='no_gate',  # or scalar or elementwise
                 attention_regularization_weight=0,  # ***
                 dec_out_sub_attend_temperature=1,
                 dec_out_sub_sigmoid_smoothing=False):

        super(NestedAttentionSeq2seq, self).__init__(
            input_size=input_size,
            encoder_type=encoder_type,
            encoder_bidirectional=encoder_bidirectional,
            encoder_num_units=encoder_num_units,
            encoder_num_proj=encoder_num_proj,
            encoder_num_layers=encoder_num_layers,
            attention_type=attention_type,
            attention_dim=attention_dim,
            decoder_type=decoder_type,
            decoder_num_units=decoder_num_units,
            decoder_num_layers=decoder_num_layers,
            embedding_dim=embedding_dim,
            dropout_input=dropout_input,
            dropout_encoder=dropout_encoder,
            dropout_decoder=dropout_decoder,
            dropout_embedding=dropout_embedding,
            num_classes=num_classes,
            parameter_init=parameter_init,
            subsample_list=subsample_list,
            subsample_type=subsample_type,
            init_dec_state=init_dec_state,
            sharpening_factor=sharpening_factor,
            logits_temperature=logits_temperature,
            sigmoid_smoothing=sigmoid_smoothing,
            coverage_weight=coverage_weight,
            ctc_loss_weight=0,
            attention_conv_num_channels=attention_conv_num_channels,
            attention_conv_width=attention_conv_width,
            num_stack=num_stack,
            splice=splice,
            conv_channels=conv_channels,
            conv_kernel_sizes=conv_kernel_sizes,
            conv_strides=conv_strides,
            poolings=poolings,
            batch_norm=batch_norm,
            scheduled_sampling_prob=scheduled_sampling_prob,
            scheduled_sampling_ramp_max_step=scheduled_sampling_ramp_max_step,
            label_smoothing_prob=label_smoothing_prob,
            weight_noise_std=weight_noise_std,
            encoder_residual=encoder_residual,
            encoder_dense_residual=encoder_dense_residual,
            decoder_residual=decoder_residual,
            decoder_dense_residual=decoder_dense_residual,
            decoding_order=decoding_order,
            bottleneck_dim=bottleneck_dim,
            num_heads=num_heads)
        self.model_type = 'nested_attention'

        # Setting for the encoder
        self.encoder_num_units_sub = encoder_num_units
        if encoder_bidirectional:
            self.encoder_num_units_sub *= 2

        # Setting for the decoder in the sub task
        if init_dec_state == 'first' and backward_sub:
            self.init_dec_state_1 = 'final'
        else:
            self.init_dec_state_1 = init_dec_state
        self.decoder_num_units_1 = decoder_num_units_sub
        self.decoder_num_layers_1 = decoder_num_layers_sub
        self.num_classes_sub = num_classes_sub + 2  # Add <EOS> class
        self.sos_1 = num_classes_sub
        self.eos_1 = num_classes_sub
        # NOTE: <SOS> and <EOS> have the same index
        self.backward_1 = backward_sub

        # Setting for the attention in the sub task
        self.num_heads_1 = num_heads_sub

        # Setting for MTL
        self.main_loss_weight = main_loss_weight
        assert sub_loss_weight > 0
        self.sub_loss_weight = sub_loss_weight
        self.ctc_loss_weight_sub = ctc_loss_weight_sub
        if scheduled_sampling_ramp_max_step == 0:
            raise ValueError('Set scheduled_sampling_ramp_max_step.')

        # Setting for decoder attention
        assert usage_dec_sub in ['update_decoder', 'all', 'no_use']
        assert gating_mechanism in ['no_gate', 'scalar', 'elementwise']
        self.usage_dec_sub = usage_dec_sub
        self.gating_mechanism = gating_mechanism
        self.attention_regularization_weight = attention_regularization_weight
        self.num_heads_dec_out_sub = num_heads_dec_out_sub

        #########################
        # Encoder
        # NOTE: overide encoder
        #########################
        if encoder_type in ['lstm', 'gru', 'rnn']:
            self.encoder = load(encoder_type=encoder_type)(
                input_size=input_size,
                rnn_type=encoder_type,
                bidirectional=encoder_bidirectional,
                num_units=encoder_num_units,
                num_proj=encoder_num_proj,
                num_layers=encoder_num_layers,
                num_layers_sub=encoder_num_layers_sub,
                dropout_input=dropout_input,
                dropout_hidden=dropout_encoder,
                subsample_list=subsample_list,
                subsample_type=subsample_type,
                batch_first=True,
                merge_bidirectional=False,
                pack_sequence=True,
                num_stack=num_stack,
                splice=splice,
                conv_channels=conv_channels,
                conv_kernel_sizes=conv_kernel_sizes,
                conv_strides=conv_strides,
                poolings=poolings,
                activation=activation,
                batch_norm=batch_norm,
                residual=encoder_residual,
                dense_residual=encoder_dense_residual)
        else:
            raise NotImplementedError

        ##################################################
        # Bridge layer between the encoder and decoder
        ##################################################
        if bridge_layer:
            self.bridge_1 = LinearND(
                self.encoder_num_units_sub, decoder_num_units_sub,
                dropout=dropout_encoder)
            self.encoder_num_units_sub = decoder_num_units_sub
            self.is_bridge_sub = True
        else:
            self.is_bridge_sub = False

        ##################################################
        # Initialization of the decoder
        ##################################################
        if encoder_type != decoder_type:
            self.init_dec_state_1 = 'zero'

        if self.init_dec_state_1 != 'zero':
            self.W_dec_init_1 = LinearND(
                self.encoder_num_units_sub, decoder_num_units_sub)

        ####################
        # Decoder (main)
        ####################
        if decoding_order == 'conditional':
            self.decoder_first_0 = RNNDecoder(
                input_size=embedding_dim,
                rnn_type=decoder_type,
                num_units=decoder_num_units,
                num_layers=decoder_num_layers,
                dropout=dropout_decoder,
                residual=False,
                dense_residual=False)
            self.decoder_second_0 = RNNDecoder(
                input_size=self.encoder_num_units * num_heads + decoder_num_units_sub,
                rnn_type=decoder_type,
                num_units=decoder_num_units,
                num_layers=decoder_num_layers,
                dropout=dropout_decoder,
                residual=False,
                dense_residual=False)
            # NOTE; the conditional decoder only supports the 1 layer
        else:
            decoder_input_size = self.encoder_num_units * num_heads + embedding_dim
            if usage_dec_sub != 'no_use':
                decoder_input_size += decoder_num_units_sub
            self.decoder_0 = RNNDecoder(
                input_size=decoder_input_size,
                rnn_type=decoder_type,
                num_units=decoder_num_units,
                num_layers=decoder_num_layers,
                dropout=dropout_decoder,
                residual=decoder_residual,
                dense_residual=decoder_dense_residual)

        ##############################
        # Embedding (sub)
        ##############################
        if label_smoothing_prob > 0:
            self.embed_1 = Embedding_LS(
                num_classes=self.num_classes_sub,
                embedding_dim=embedding_dim_sub,
                dropout=dropout_embedding,
                label_smoothing_prob=label_smoothing_prob)
        else:
            self.embed_1 = Embedding(
                num_classes=self.num_classes_sub,
                embedding_dim=embedding_dim_sub,
                dropout=dropout_embedding,
                ignore_index=-1)

        ##############################
        # Decoder (sub)
        ##############################
        if decoding_order == 'conditional':
            self.decoder_first_1 = RNNDecoder(
                input_size=embedding_dim_sub,
                rnn_type=decoder_type,
                num_units=decoder_num_units_sub,
                num_layers=1,
                dropout=dropout_decoder,
                residual=False,
                dense_residual=False)
            self.decoder_second_1 = RNNDecoder(
                input_size=self.encoder_num_units_sub * num_heads_sub,
                rnn_type=decoder_type,
                num_units=decoder_num_units_sub,
                num_layers=1,
                dropout=dropout_decoder,
                residual=False,
                dense_residual=False)
            # NOTE; the conditional decoder only supports the 1 layer
        else:
            self.decoder_1 = RNNDecoder(
                input_size=self.encoder_num_units_sub * num_heads_sub + embedding_dim_sub,
                rnn_type=decoder_type,
                num_units=decoder_num_units_sub,
                num_layers=decoder_num_layers_sub,
                dropout=dropout_decoder,
                residual=decoder_residual,
                dense_residual=decoder_dense_residual)

        ###################################
        # Attention layer (sub)
        ###################################
        self.attend_1 = AttentionMechanism(
            encoder_num_units=self.encoder_num_units_sub,
            decoder_num_units=decoder_num_units_sub,
            attention_type=attention_type,
            attention_dim=attention_dim,
            sharpening_factor=sharpening_factor,
            sigmoid_smoothing=sigmoid_smoothing,
            out_channels=attention_conv_num_channels,
            kernel_size=attention_conv_width,
            num_heads=num_heads_sub)

        ##############################
        # Output layer (sub)
        ##############################
        self.W_d_1 = LinearND(
            decoder_num_units_sub, bottleneck_dim_sub,
            dropout=dropout_decoder)
        self.W_c_1 = LinearND(
            self.encoder_num_units_sub * num_heads_sub, bottleneck_dim_sub,
            dropout=dropout_decoder)
        self.fc_1 = LinearND(bottleneck_dim_sub, self.num_classes_sub)

        ############################################################
        # Attention layer (to the decoder states in the sub task)
        ############################################################
        self.attend_dec_sub = AttentionMechanism(
            encoder_num_units=decoder_num_units,
            decoder_num_units=decoder_num_units,
            attention_type='content',
            # attention_type='location',
            attention_dim=attention_dim,
            sharpening_factor=1 / dec_out_sub_attend_temperature,
            sigmoid_smoothing=dec_out_sub_sigmoid_smoothing,
            out_channels=attention_conv_num_channels,
            kernel_size=21,
            num_heads=num_heads_dec_out_sub)

        ##############################################
        # Usage of decoder states in the sub task
        ##############################################
        if usage_dec_sub == 'all':
            self.W_c_dec_sub = LinearND(
                decoder_num_units, decoder_num_units,
                dropout=dropout_decoder)
        elif usage_dec_sub == 'update_decoder':
            pass

        ##############################################
        # Gating of decoder states in the sub task
        ##############################################
        if gating_mechanism == 'scalar':
            self.gate_fn = LinearND(
                decoder_num_units + decoder_num_units_sub, 1,
                dropout=dropout_decoder)
        elif gating_mechanism == 'elementwise':
            self.gate_fn = LinearND(
                decoder_num_units + decoder_num_units_sub, decoder_num_units,
                dropout=dropout_decoder)
            assert decoder_num_units == decoder_num_units
        elif gating_mechanism == 'no_gate':
            pass

        ##############################
        # CTC (sub)
        ##############################
        if ctc_loss_weight_sub > 0:
            self.fc_ctc_1 = LinearND(
                self.encoder_num_units_sub, num_classes_sub + 1)

            # Set CTC decoders
            self._decode_ctc_greedy_np = GreedyDecoder(blank_index=0)
            self._decode_ctc_beam_np = BeamSearchDecoder(blank_index=0)
            # NOTE: index 0 is reserved for the blank class

        ##################################################
        # Initialize parameters
        ##################################################
        self.init_weights(parameter_init,
                          distribution=parameter_init_distribution,
                          ignore_keys=['bias'])

        # Initialize all biases with 0
        self.init_weights(0, distribution='constant', keys=['bias'])

        # Recurrent weights are orthogonalized
        if recurrent_weight_orthogonal:
            self.init_weights(parameter_init,
                              distribution='orthogonal',
                              keys=[encoder_type, 'weight'],
                              ignore_keys=['bias'])
            self.init_weights(parameter_init,
                              distribution='orthogonal',
                              keys=[decoder_type, 'weight'],
                              ignore_keys=['bias'])

        # Initialize bias in forget gate with 1
        if init_forget_gate_bias_with_one:
            self.init_forget_gate_bias_with_one()

    def forward(self, xs, ys, ys_sub, x_lens, y_lens, y_lens_sub, is_eval=False):
        """Forward computation.
        Args:
            xs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            ys (np.ndarray): A tensor of size `[B, T_out]`
            ys_sub (np.ndarray): A tensor of size `[B, T_out_sub]`
            x_lens (np.ndarray): A tensor of size `[B]`
            y_lens (np.ndarray): A tensor of size `[B]`
            y_lens_sub (np.ndarray): A tensor of size `[B]`
            is_eval (bool, optional): if True, the history will not be saved.
                This should be used in inference model for memory efficiency.
        Returns:
            loss (torch.autograd.Variable(float) or float): A tensor of size `[1]`
            loss_main (torch.autograd.Variable(float) or float): A tensor of size `[1]`
            loss_sub (torch.autograd.Variable(float) or float): A tensor of size `[1]`
        """
        if is_eval:
            self.eval()
        else:
            self.train()

            # Gaussian noise injection
            if self.weight_noise_injection:
                self.inject_weight_noise(mean=0, std=self.weight_noise_std)

        # Reverse the order
        if self.backward_1:
            ys_sub_tmp = copy.deepcopy(ys_sub)
            for b in range(len(xs)):
                ys_sub_tmp[b, :y_lens_sub[b]] = ys_sub[b, :y_lens_sub[b]][::-1]
        else:
            ys_sub_tmp = ys_sub

        # NOTE: ys and ys_sub are padded with -1 here
        # ys_in and ys_in_sub areb padded with <EOS> in order to convert to
        # one-hot vector, and added <SOS> before the first token
        # ys_out and ys_out_sub are padded with -1, and added <EOS>
        # after the last token
        ys_in = self._create_var((ys.shape[0], ys.shape[1] + 1),
                                 fill_value=self.eos_0, dtype='long')
        ys_in_sub = self._create_var((ys_sub.shape[0], ys_sub.shape[1] + 1),
                                     fill_value=self.eos_1, dtype='long')
        ys_out = self._create_var((ys.shape[0], ys.shape[1] + 1),
                                  fill_value=-1, dtype='long')
        ys_out_sub = self._create_var((ys_sub.shape[0], ys_sub.shape[1] + 1),
                                      fill_value=-1, dtype='long')
        for b in range(len(xs)):
            ys_in.data[b, 0] = self.sos_0
            ys_in.data[b, 1:y_lens[b] + 1] = torch.from_numpy(
                ys[b, :y_lens[b]])
            ys_in_sub.data[b, 0] = self.sos_1
            ys_in_sub.data[b, 1:y_lens_sub[b] + 1] = torch.from_numpy(
                ys_sub_tmp[b, :y_lens_sub[b]])

            ys_out.data[b, :y_lens[b]] = torch.from_numpy(ys[b, :y_lens[b]])
            ys_out.data[b, y_lens[b]] = self.eos_0
            ys_out_sub.data[b, :y_lens_sub[b]] = torch.from_numpy(
                ys_sub_tmp[b, :y_lens_sub[b]])
            ys_out_sub.data[b, y_lens_sub[b]] = self.eos_1

        if self.use_cuda:
            ys_in = ys_in.cuda()
            ys_in_sub = ys_in_sub.cuda()
            ys_out = ys_out.cuda()
            ys_out_sub = ys_out_sub.cuda()

        # Wrap by Variable
        xs = self.np2var(xs)
        x_lens = self.np2var(x_lens, dtype='int')
        y_lens = self.np2var(y_lens, dtype='int')
        y_lens_sub = self.np2var(y_lens_sub, dtype='int')

        # Encode acoustic features
        xs, x_lens, xs_sub, x_lens_sub, perm_idx = self._encode(
            xs, x_lens, is_multi_task=True)

        # Permutate indices
        if perm_idx is not None:
            ys_in = ys_in[perm_idx]
            ys_out = ys_out[perm_idx]
            y_lens = y_lens[perm_idx]

            ys_in_sub = ys_in_sub[perm_idx]
            ys_out_sub = ys_out_sub[perm_idx]
            y_lens_sub = y_lens_sub[perm_idx]

        ##################################################
        # Main + Sub task (attention)
        ##################################################
        # Compute XE loss
        loss_main, loss_sub = self.compute_xe_loss(
            xs, ys_in, ys_out, x_lens, y_lens,
            xs_sub, ys_in_sub, ys_out_sub, x_lens_sub, y_lens_sub)

        loss_main = loss_main * self.main_loss_weight
        loss_sub = loss_sub * self.sub_loss_weight
        loss = loss_main + loss_sub

        ##################################################
        # Sub task (CTC, optional)
        ##################################################
        if self.ctc_loss_weight_sub > 0:
            ctc_loss_sub = self.compute_ctc_loss(
                xs_sub, ys_in_sub[:, 1:] + 1,
                x_lens_sub, y_lens_sub, task_idx=1)

            ctc_loss_sub = ctc_loss_sub * self.ctc_loss_weight_sub
            loss += ctc_loss_sub

        if is_eval:
            loss = loss.data[0]
            loss_main = loss_main.data[0]
            loss_sub = loss_sub.data[0]
        else:
            # Update the probability of scheduled sampling
            self._step += 1
            if self.sample_prob > 0:
                self._sample_prob = min(
                    self.sample_prob,
                    self.sample_prob / self.sample_ramp_max_step * self._step)

        return loss, loss_main, loss_sub

    def compute_xe_loss(self, enc_out, ys_in, ys_out, x_lens, y_lens,
                        enc_out_sub, ys_in_sub, ys_out_sub, x_lens_sub, y_lens_sub):
        """Compute XE loss.
        Args:
            enc_out (torch.autograd.Variable, float): A tensor of size
                `[B, T_in, encoder_num_units]`
            ys_in (torch.autograd.Variable, long): A tensor of size
                `[B, T_out]`, which includes <SOS>
            ys_out (torch.autograd.Variable, long): A tensor of size
                `[B, T_out]`, which includes <EOS>
            x_lens (torch.autograd.Variable, int): A tensor of size `[B]`
            y_lens (torch.autograd.Variable, int): A tensor of size `[B]`

            enc_out_sub (torch.autograd.Variable, float): A tensor of size
                `[B, T_in_sub, encoder_num_units]`
            ys_in_sub (torch.autograd.Variable, long): A tensor of size
                `[B, T_out_sub]`, which includes <SOS>
            ys_out_sub (torch.autograd.Variable, long): A tensor of size
                `[B, T_out_sub]`, which includes <EOS>
            x_lens_sub (torch.autograd.Variable, int): A tensor of size `[B]`
            y_lens_sub (torch.autograd.Variable, int): A tensor of size `[B]`
        Returns:
            loss_main (torch.autograd.Variable, float): A tensor of size `[1]`
            loss_sub (torch.autograd.Variable, float): A tensor of size `[1]`
        """
        # Teacher-forcing
        logits_main, aw, logits_sub, aw_sub, aw_dec_out_sub = self._decode_train(
            enc_out, x_lens, ys_in,
            enc_out_sub, x_lens_sub, ys_in_sub, y_lens_sub)

        ##################################################
        # Main task
        ##################################################
        if self.main_loss_weight > 0:
            # Output smoothing
            if self.logits_temperature != 1:
                logits_main /= self.logits_temperature

            # Compute XE sequence loss in the main task
            loss_main = F.cross_entropy(
                input=logits_main.view((-1, logits_main.size(2))),
                target=ys_out.view(-1),
                ignore_index=-1, size_average=False) / len(enc_out)

            # Label smoothing (with uniform distribution)
            if self.label_smoothing_prob > 0:
                loss_ls_main = cross_entropy_label_smoothing(
                    logits_main,
                    y_lens=y_lens + 1,  # Add <EOS>
                    label_smoothing_prob=self.label_smoothing_prob,
                    distribution='uniform',
                    size_average=True)
                loss_main = loss_main * \
                    (1 - self.label_smoothing_prob) + loss_ls_main

            # Attention regularization
            if self.attention_regularization_weight > 0:
                if self.backward_1:
                    raise NotImplementedError

                loss_main += F.mse_loss(
                    torch.bmm(aw_dec_out_sub, aw_sub),
                    aw.detach(),
                    # aw.clone(),
                    size_average=True, reduce=True) * self.attention_regularization_weight
        else:
            loss_main = self._create_var((1,), fill_value=0)

        ##################################################
        # Sub task
        ##################################################
        # Output smoothing
        if self.logits_temperature != 1:
            logits_sub /= self.logits_temperature

        # Compute XE sequence loss in the sub task
        loss_sub = F.cross_entropy(
            input=logits_sub.view((-1, logits_sub.size(2))),
            target=ys_out_sub.view(-1),
            ignore_index=-1, size_average=False) / len(enc_out_sub)

        # Label smoothing (with uniform distribution)
        if self.label_smoothing_prob > 0:
            loss_ls_sub = cross_entropy_label_smoothing(
                logits_sub,
                y_lens=y_lens_sub + 1,  # Add <EOS>
                label_smoothing_prob=self.label_smoothing_prob,
                distribution='uniform',
                size_average=True)
            loss_sub = loss_sub * (1 - self.label_smoothing_prob) + loss_ls_sub

        # Add coverage term
        if self.coverage_weight != 0:
            raise NotImplementedError

        return loss_main, loss_sub

    def _decode_train(self, enc_out, x_lens, ys,
                      enc_out_sub, x_lens_sub, ys_sub, y_lens_sub):
        """Decoding in the training stage.
        Args:
            enc_out (torch.autograd.Variable, float): A tensor of size
                `[B, T_in, encoder_num_units]`
            x_lens (torch.autograd.Variable, int): A tensor of size `[B]`
            ys (torch.autograd.Variable, long): A tensor of size `[B, T_out]`

            enc_out_sub (torch.autograd.Variable, float): A tensor of size
                `[B, T_in_sub, encoder_num_units]`
            x_lens_sub (torch.autograd.Variable, int): A tensor of size `[B]`
            ys_sub (torch.autograd.Variable, long): A tensor of size `[B, T_out_sub]`
            y_lens_sub (torch.autograd.Variable, long): A tensor of size `[B]`
        Returns:
            logits (torch.autograd.Variable, float): A tensor of size
                `[B, T_out, num_classes]`
            aw (torch.autograd.Variable, float): A tensor of size
                `[B, T_out, T_in]`
            logits_sub (torch.autograd.Variable, float): A tensor of size
                `[B, T_out_sub, num_classes_sub]`
            aw_sub (torch.autograd.Variable, float): A tensor of size
                `[B, T_out_sub, T_in_sub]`
            aw_dec_out_sub (np.ndarray): A tensor of size
                `[B, T_out, T_out_sub]`
        """
        batch_size, max_time = enc_out.size()[:2]
        max_time_sub = enc_out_sub.size(1)

        ##################################################
        # At first, compute logits of the character model
        ##################################################
        # Initialization for the character model
        dec_state_sub, dec_out_sub = self._init_decoder_state(
            enc_out_sub, task_idx=1)
        aw_step_sub = self._create_var(
            (batch_size, max_time_sub, self.num_heads_1), fill_value=0)

        dec_out_sub_seq = []
        logits_sub = []
        aw_sub = []
        for t in range(ys_sub.size(1)):

            # for scheduled sampling
            is_sample = self.sample_prob > 0 and t > 0 and self._step > 0 and random.random(
            ) < self._sample_prob

            if self.decoding_order == 'attend_update_generate':
                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, dec_out_sub, aw_step_sub)

                # Sample
                y_sub = torch.max(
                    logits_sub[-1], dim=2)[1] if is_sample else ys_sub[:, t:t + 1]
                y_sub = self.embed_1(y_sub)

                # Recurrency
                dec_in_sub = torch.cat([y_sub, context_vec_sub], dim=-1)
                dec_out_sub, dec_state_sub = self.decoder_1(
                    dec_in_sub, dec_state_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

            elif self.decoding_order == 'attend_generate_update':
                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, dec_out_sub, aw_step_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

                if t < ys_sub.size(1) - 1:
                    # Sample
                    y_sub = torch.max(
                        logits_step_sub, dim=2)[1] if is_sample else ys_sub[:, t + 1:t + 2]
                    y_sub = self.embed_1(y_sub)

                    # Recurrency
                    dec_in_sub = torch.cat([y_sub, context_vec_sub], dim=-1)
                    dec_out_sub, dec_state_sub = self.decoder_1(
                        dec_in_sub, dec_state_sub)

            elif self.decoding_order == 'conditional':
                # Sample
                y_sub = torch.max(
                    logits_sub[-1], dim=2)[1] if is_sample else ys_sub[:, t:t + 1]
                y_sub = self.embed_1(y_sub)

                # Recurrency of the first decoder
                _dec_out_sub, _dec_state_sub = self.decoder_first_1(
                    y_sub, dec_state_sub)

                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, _dec_out_sub, aw_step_sub)

                # Recurrency of the second decoder
                dec_out_sub, dec_state_sub = self.decoder_second_1(
                    context_vec_sub, _dec_state_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

            dec_out_sub_seq.append(dec_out_sub)

            logits_sub.append(logits_step_sub)
            aw_sub.append(aw_step_sub)

        # Concatenate in T_out-dimension
        dec_out_sub_seq = torch.cat(dec_out_sub_seq, dim=1)
        logits_sub = torch.cat(logits_sub, dim=1)
        aw_sub = torch.stack(aw_sub, dim=1)

        if self.main_loss_weight == 0:
            return None, None, logits_sub, aw_sub, None

        ##################################################
        # Next, compute logits of the word model
        ##################################################
        # Initialization for the word model
        dec_state, dec_out = self._init_decoder_state(enc_out, task_idx=0)
        aw_step = self._create_var(
            (batch_size, max_time, self.num_heads_0), fill_value=0)
        aw_dec_out_sub_step = self._create_var(
            (batch_size, dec_out_sub_seq.size(1), self.num_heads_dec_out_sub),
            fill_value=0)

        logits = []
        aw = []
        aw_dec_out_sub = []
        for t in range(ys.size(1)):

            is_sample = self.sample_prob > 0 and t > 0 and self._step > 0 and random.random(
            ) < self._sample_prob

            if self.decoding_order == 'attend_update_generate':
                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, dec_out, aw_dec_out_sub_step)

                # Sample
                y = torch.max(
                    logits[-1], dim=2)[1] if is_sample else ys[:, t:t + 1]
                y = self.embed_0(y)

                # Recurrency
                dec_in = torch.cat([y, context_vec], dim=-1)
                if self.usage_dec_sub != 'no_use':
                    # Compute the importance of character-level decoder states
                    if self.gating_mechanism != 'no_gate':
                        gate = F.sigmoid(self.gate_fn(
                            torch.cat([dec_out, context_vec_dec_out_sub], dim=-1)))
                        context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                    dec_in = torch.cat(
                        [dec_in, context_vec_dec_out_sub], dim=-1)
                dec_out, dec_state = self.decoder_0(dec_in, dec_state)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

            elif self.decoding_order == 'attend_generate_update':
                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, dec_out, aw_dec_out_sub_step)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

                if t < ys.size(1) - 1:
                    # Sample
                    y = torch.max(
                        logits_step, dim=2)[1] if is_sample else ys[:, t + 1:t + 2]
                    y = self.embed_0(y)

                    # Recurrency
                    dec_in = torch.cat([y, context_vec], dim=-1)
                    if self.usage_dec_sub != 'no_use':
                        # Compute the importance of character-level decoder states
                        if self.gating_mechanism != 'no_gate':
                            gate = F.sigmoid(self.gate_fn(
                                torch.cat([dec_out, context_vec_dec_out_sub], dim=-1)))
                            context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                        dec_in = torch.cat(
                            [dec_in, context_vec_dec_out_sub], dim=-1)
                    dec_out, dec_state = self.decoder_0(dec_in, dec_state)

            elif self.decoding_order == 'conditional':
                # Sample
                y = torch.max(
                    logits[-1], dim=2)[1] if is_sample else ys[:, t:t + 1]
                y = self.embed_0(y)

                # Recurrency of the first decoder
                _dec_out, _dec_state = self.decoder_first_0(y, dec_state)

                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, _dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, _dec_out, aw_dec_out_sub_step)

                # Recurrency
                dec_in = torch.cat([y, context_vec], dim=-1)
                if self.usage_dec_sub != 'no_use':
                    # Compute the importance of character-level decoder states
                    if self.gating_mechanism != 'no_gate':
                        gate = F.sigmoid(self.gate_fn(
                            torch.cat([_dec_out, context_vec_dec_out_sub], dim=-1)))
                        context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                    dec_in = torch.cat(
                        [dec_in, context_vec_dec_out_sub], dim=-1)
                dec_out, dec_state = self.decoder_0(dec_in, _dec_state)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

            logits.append(logits_step)
            aw.append(aw_step)
            aw_dec_out_sub.append(aw_dec_out_sub_step)

        # Concatenate in T_out-dimension
        logits = torch.cat(logits, dim=1)
        aw = torch.stack(aw, dim=1)
        aw_dec_out_sub = torch.stack(aw_dec_out_sub, dim=1)
        # NOTE; aw in the training stage may be used for computing the
        # coverage, so do not convert to numpy yet.

        # TODO: fix these
        aw = aw.squeeze(3)
        aw_sub = aw_sub.squeeze(3)
        aw_dec_out_sub = aw_dec_out_sub.squeeze(3)

        return logits, aw, logits_sub, aw_sub, aw_dec_out_sub

    def attention_weights(self, xs, x_lens, max_decode_len, max_decode_len_sub):
        """Get attention weights for visualization.
        Args:
            xs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            x_lens (np.ndarray): A tensor of size `[B]`
            max_decode_len (int): the length of output sequences
                to stop prediction when EOS token have not been emitted
        Returns:
            best_hyps (np.ndarray): A tensor of size `[B, T_out]`
            best_hyps_sub (np.ndarray): A tensor of size `[B, T_out_sub]`
            aw (np.ndarray): A tensor of size `[B, T_out, T_in]`
            aw_sub (np.ndarray): A tensor of size `[B, T_out_sub, T_in]`
            aw_dec_out_sub (np.ndarray): A tensor of size
                `[B, T_out, T_out_sub]`
            gate_weights (np.ndarray):  A tensor of size
                `[B, decoder_num_units or 1, T_out]`
        """
        # Change to evaluation mode
        self.eval()

        # Wrap by Variable
        xs = self.np2var(xs)
        x_lens = self.np2var(x_lens, dtype='int')

        # Encode acoustic features
        enc_out, x_lens, enc_out_sub, x_lens_sub, perm_idx = self._encode(
            xs, x_lens, is_multi_task=True)

        best_hyps, aw, best_hyps_sub, aw_sub, aw_dec_out_sub, gate_weights = self._decode_infer_greedy_joint(
            enc_out, x_lens, enc_out_sub, x_lens_sub,
            beam_width=1,
            max_decode_len=max_decode_len,
            max_decode_len_sub=max_decode_len_sub)

        # Permutate indices to the original order
        if perm_idx is None:
            perm_idx = np.arange(0, len(xs), 1)
        else:
            perm_idx = self.var2np(perm_idx)

        return best_hyps, best_hyps_sub, aw, aw_sub, aw_dec_out_sub, gate_weights

    def decode(self, xs, x_lens, beam_width, max_decode_len, max_decode_len_sub=None,
               length_penalty=0, coverage_penalty=0, task_index=0,
               resolving_unk=False, teacher_forcing=False):
        """Decoding in the inference stage.
        Args:
            xs (np.ndarray): A tensor of size `[B, T_in, input_size]`
            x_lens (np.ndarray): A tensor of size `[B]`
            beam_width (int): the size of beam
            max_decode_len (int): the length of output sequences
                to stop prediction when EOS token have not been emitted
            max_decode_len_sub (int, optional):
            length_penalty (float, optional):
            coverage_penalty (float, optional):
            task_index (int, optional): the index of a task
            resolving_unk (bool, optional):
        Returns:
            best_hyps (np.ndarray): A tensor of size `[B]`
            best_hyps_sub (np.ndarray): A tensor of size `[B]`
            perm_idx (np.ndarray): A tensor of size `[B]`
        """
        # Change to evaluation mode
        self.eval()

        # Wrap by Variable
        xs = self.np2var(xs)
        x_lens = self.np2var(x_lens, dtype='int')

        # Encode acoustic features
        if task_index == 0:
            enc_out, x_lens, enc_out_sub, x_lens_sub, perm_idx = self._encode(
                xs, x_lens, is_multi_task=True)

            # Next, decode by word-based decoder with character outputs
            if beam_width == 1:
                best_hyps, aw, best_hyps_sub, aw_sub, _, _ = self._decode_infer_greedy_joint(
                    enc_out, x_lens, enc_out_sub, x_lens_sub,
                    beam_width=1,
                    max_decode_len=max_decode_len,
                    max_decode_len_sub=max_decode_len_sub)
            else:
                raise NotImplementedError
        elif task_index == 1:
            _, _, enc_out, x_lens, perm_idx = self._encode(
                xs, x_lens, is_multi_task=True)

            if beam_width == 1:
                best_hyps, _ = self._decode_infer_greedy(
                    enc_out, x_lens, max_decode_len, task_idx=1)
            else:
                best_hyps, _ = self._decode_infer_beam(
                    enc_out, x_lens, max_decode_len, task_idx=1)
        else:
            raise ValueError

        # Permutate indices to the original order
        if perm_idx is None:
            perm_idx = np.arange(0, len(xs), 1)
        else:
            perm_idx = self.var2np(perm_idx)

        if task_index == 0:
            if resolving_unk:
                return best_hyps, aw, best_hyps_sub, aw_sub, perm_idx
            else:
                return best_hyps, best_hyps_sub, perm_idx
        elif task_index == 1:
            return best_hyps, perm_idx

    def _decode_infer_greedy_joint(self, enc_out, x_lens,
                                   enc_out_sub, x_lens_sub, beam_width,
                                   max_decode_len, max_decode_len_sub):
        """Greedy decoding in the inference stage.
        Args:
            enc_out (torch.autograd.Variable, float): A tensor of size
                `[B, T_in, encoder_num_units]`
            x_lens (torch.autograd.Variable, int): A tensor of size `[B]`
            enc_out_sub (torch.autograd.Variable, float): A tensor of size
                `[B, T_in_sub, encoder_num_units]`
            x_lens_sub (torch.autograd.Variable, int): A tensor of size `[B]`
            beam_width (int): the size of beam
            max_decode_len (int): the length of output sequences
                to stop prediction when EOS token have not been emitted
            max_decode_len_sub (int): the length of output sequences
                to stop prediction when EOS token have not been emitted
        Returns:
            best_hyps (np.ndarray): A tensor of size `[B, T_out]`
            aw (np.ndarray): A tensor of size `[B, T_out, T_in]`
            best_hyps_sub (np.ndarray): A tensor of size `[B, T_out_sub]`
            aw_sub (np.ndarray): A tensor of size `[B, T_out_sub, T_in]`
            aw_dec_out_sub (np.ndarray): A tensor of size
                `[B, T_out, T_out_sub]`
            gate_weights (np.ndarray):  A tensor of size
                `[B, decoder_num_units or 1, T_out]`
        """
        batch_size, max_time = enc_out.size()[:2]
        max_time_sub = enc_out_sub.size(1)

        ##################################################
        # At first, decode by the character model
        ##################################################
        # Initialization for the character model
        dec_state_sub, dec_out_sub = self._init_decoder_state(
            enc_out_sub, task_idx=1)
        aw_step_sub = self._create_var(
            (batch_size, max_time_sub, self.num_heads_1),
            fill_value=0, volatile=True)

        # Start from <SOS>
        y_sub = self._create_var(
            (batch_size, 1), fill_value=self.sos_1, dtype='long')
        y_emb_sub = self.embed_1(y_sub)

        dec_out_sub_seq = []
        aw_sub = []
        best_hyps_sub = []
        y_lens_sub = np.zeros((batch_size,), dtype=np.int32)
        eos_flag = [False] * batch_size

        for t in range(max_decode_len_sub):

            if self.decoding_order == 'attend_update_generate':
                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, dec_out_sub, aw_step_sub)

                # Recurrency
                dec_in_sub = torch.cat([y_emb_sub, context_vec_sub], dim=-1)
                dec_out_sub, dec_state_sub = self.decoder_1(
                    dec_in_sub, dec_state_sub)
                dec_out_sub_seq.append(dec_out_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

                # Pick up 1-best
                y_sub = torch.max(logits_step_sub.squeeze(1), dim=1)[
                    1].unsqueeze(1)
                best_hyps_sub.append(y_sub)
                y_emb_sub = self.embed_1(y_sub)

            elif self.decoding_order == 'attend_generate_update':
                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, dec_out_sub, aw_step_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

                # Pick up 1-best
                y_sub = torch.max(logits_step_sub.squeeze(1), dim=1)[
                    1].unsqueeze(1)
                best_hyps_sub.append(y_sub)
                y_emb_sub = self.embed_1(y_sub)

                # Recurrency
                dec_in_sub = torch.cat([y_emb_sub, context_vec_sub], dim=-1)
                dec_out_sub, dec_state_sub = self.decoder_1(
                    dec_in_sub, dec_state_sub)
                dec_out_sub_seq.append(dec_out_sub)

            elif self.decoding_order == 'conditional':
                # Recurrency of the first decoder
                _dec_out_sub, _dec_state_sub = self.decoder_first_1(
                    y_sub, dec_state_sub)

                # Score
                context_vec_sub, aw_step_sub = self.attend_1(
                    enc_out_sub, x_lens_sub, _dec_out_sub, aw_step_sub)

                # Recurrency of the second decoder
                dec_out_sub, dec_state_sub = self.decoder_second_1(
                    context_vec_sub, _dec_state_sub)
                dec_out_sub_seq.append(dec_out_sub)

                # Generate
                logits_step_sub = self.fc_1(F.tanh(
                    self.W_d_1(dec_out_sub) +
                    self.W_c_1(context_vec_sub)))

                # Pick up 1-best
                y_sub = torch.max(logits_step_sub.squeeze(1), dim=1)[
                    1].unsqueeze(1)
                best_hyps_sub.append(y_sub)
                y_emb_sub = self.embed_1(y_sub)

            aw_sub.append(aw_step_sub)

            for b in range(batch_size):
                if not eos_flag[b]:
                    y_lens_sub[b] += 1
                    if y_sub.data.cpu().numpy()[b] == self.eos_1:
                        eos_flag[b] = True

            # Break if <EOS> is outputed in all mini-batch
            if torch.sum(y_sub.data == self.eos_1) == y_sub.numel():
                break

        # Concatenate in T_out dimension
        dec_out_sub_seq = torch.cat(dec_out_sub_seq, dim=1)
        best_hyps_sub = torch.cat(best_hyps_sub, dim=1)
        aw_sub = torch.stack(aw_sub, dim=1)

        ##################################################
        # Next, compute logits of the word model
        ##################################################
        # Initialization for the word model
        dec_state, dec_out = self._init_decoder_state(enc_out, task_idx=0)
        aw_step = self._create_var(
            (batch_size, max_time, self.num_heads_0),
            fill_value=0, volatile=True)
        aw_dec_out_sub_step = self._create_var(
            (batch_size, dec_out_sub_seq.size(1), self.num_heads_dec_out_sub),
            volatile=True)

        y_lens_sub = self.np2var(y_lens_sub + 1, dtype='int')
        # NOTE: add <SOS>
        # assert max(y_lens_sub.data) > 0

        # Start from <SOS>
        y = self._create_var(
            (batch_size, 1), fill_value=self.sos_0, dtype='long')
        y_emb = self.embed_0(y)

        best_hyps = []
        aw = []
        aw_dec_out_sub = []
        gate_weights = []
        for _ in range(max_decode_len):

            if self.decoding_order == 'attend_update_generate':
                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, dec_out, aw_dec_out_sub_step)

                # Recurrency
                dec_in = torch.cat([y_emb, context_vec], dim=-1)
                if self.usage_dec_sub != 'no_use':
                    # Compute the importance of character-level decoder states
                    if self.gating_mechanism != 'no_gate':
                        gate = F.sigmoid(self.gate_fn(
                            torch.cat([dec_out, context_vec_dec_out_sub], dim=-1)))
                        context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                        gate_weights.append(gate)
                    dec_in = torch.cat(
                        [dec_in, context_vec_dec_out_sub], dim=-1)
                dec_out, dec_state = self.decoder_0(dec_in, dec_state)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

                # Pick up 1-best
                y = torch.max(logits_step.squeeze(1), dim=1)[1].unsqueeze(1)
                best_hyps.append(y)
                y_emb = self.embed_0(y)

            elif self.decoding_order == 'attend_generate_update':
                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, dec_out, aw_dec_out_sub_step)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

                # Pick up 1-best
                y = torch.max(logits_step.squeeze(1), dim=1)[1].unsqueeze(1)
                best_hyps.append(y)
                y_emb = self.embed_0(y)

                # Recurrency
                dec_in = torch.cat([y_emb, context_vec], dim=-1)
                if self.usage_dec_sub != 'no_use':
                    # Compute the importance of character-level decoder states
                    if self.gating_mechanism != 'no_gate':
                        gate = F.sigmoid(self.gate_fn(
                            torch.cat([dec_out, context_vec_dec_out_sub], dim=-1)))
                        context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                        gate_weights.append(gate)
                    dec_in = torch.cat(
                        [dec_in, context_vec_dec_out_sub], dim=-1)
                dec_out, dec_state = self.decoder_0(dec_in, dec_state)

            elif self.decoding_order == 'conditional':
                # Recurrency of the first decoder
                _dec_out, _dec_state = self.decoder_first_0(y, dec_state)

                # Score for the encoder
                context_vec, aw_step = self.attend_0(
                    enc_out, x_lens, _dec_out, aw_step)

                # Score for the character-level decoder states
                context_vec_dec_out_sub, aw_dec_out_sub_step = self.attend_dec_sub(
                    dec_out_sub_seq, y_lens_sub, _dec_out, aw_dec_out_sub_step)

                # Recurrency
                dec_in = torch.cat([y_emb, context_vec], dim=-1)
                if self.usage_dec_sub != 'no_use':
                    # Compute the importance of character-level decoder states
                    if self.gating_mechanism != 'no_gate':
                        gate = F.sigmoid(self.gate_fn(
                            torch.cat([_dec_out, context_vec_dec_out_sub], dim=-1)))
                        context_vec_dec_out_sub = gate * context_vec_dec_out_sub
                        gate_weights.append(gate)
                    dec_in = torch.cat(
                        [dec_in, context_vec_dec_out_sub], dim=-1)
                dec_out, dec_state = self.decoder_0(dec_in, _dec_state)

                # Generate
                if self.usage_dec_sub == 'all':
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec) + \
                        self.W_c_dec_sub(context_vec_dec_out_sub)
                else:
                    out = self.W_d_0(dec_out) + self.W_c_0(context_vec)
                logits_step = self.fc_0(F.tanh(out))

            aw.append(aw_step)
            aw_dec_out_sub.append(aw_dec_out_sub_step)

            # Break if <EOS> is outputed in all mini-batch
            if torch.sum(y.data == self.eos_0) == y.numel():
                break

        # Concatenate in T_out dimension
        best_hyps = torch.cat(best_hyps, dim=1)
        aw = torch.stack(aw, dim=1)
        aw_dec_out_sub = torch.stack(aw_dec_out_sub, dim=1)

        # Convert to numpy
        best_hyps = self.var2np(best_hyps)
        aw = self.var2np(aw)
        best_hyps_sub = self.var2np(best_hyps_sub)
        aw_sub = self.var2np(aw_sub)
        aw_dec_out_sub = self.var2np(aw_dec_out_sub)

        # Reverse the order
        if self.backward_1:
            for b in range(batch_size):
                best_hyps_sub[b, :y_lens_sub[b]
                              ] = best_hyps_sub[b, :y_lens_sub[b]][::-1]

        if self.gating_mechanism != 'no_gate':
            gate_weights = torch.stack(gate_weights, dim=1).squeeze(2)
            gate_weights = self.var2np(gate_weights)
        else:
            gate_weights = None

        return best_hyps, aw, best_hyps_sub, aw_sub, aw_dec_out_sub, gate_weights