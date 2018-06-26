#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""Test RNN language models (pytorch)."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import sys
import time
import unittest
import math
import argparse

import torch
torch.manual_seed(1623)
torch.cuda.manual_seed_all(1623)

sys.path.append('../../../../../')
from src.models.pytorch_v3.lm.rnnlm import RNNLM
from src.models.pytorch_v3.data_parallel import CustomDataParallel
from src.models.test.data import generate_data, idx2char, idx2word
from src.utils.measure_time_func import measure_time
from src.bin.training.utils.learning_rate_controller import Controller

parser = argparse.ArgumentParser()
parser.add_argument('--ngpus', type=int, default=0,
                    help='the number of GPUs (negative value indicates CPU)')
args = parser.parse_args()


class TestRNNLM(unittest.TestCase):

    def test(self):
        print("RNNLM Working check.")

        # bidirectional
        self.check(rnn_type='lstm', bidirectional=True)
        self.check(rnn_type='gru', bidirectional=True)

        # residual connection
        self.check(rnn_type='lstm', residual_connection=True)

        # label smoothing
        self.check(rnn_type='lstm', label_smoothing_prob=0.1)

        # backward
        self.check(rnn_type='lstm', bidirectional=False, backward=True)

        # unidirectional
        self.check(rnn_type='lstm', bidirectional=False)
        self.check(rnn_type='gru', bidirectional=False)

        # Tie weights
        # self.check(rnn_type='lstm', bidirectional=False,
        #            tie_weights=True)

        # character-level LM
        self.check(rnn_type='lstm', bidirectional=True,
                   label_type='word', backward=False)

    @measure_time
    def check(self, rnn_type, bidirectional=False,
              label_type='word', tie_weights=False, residual_connection=False,
              backward=False, label_smoothing_prob=0, bptt=35):

        print('==================================================')
        print('  label_type: %s' % label_type)
        print('  rnn_type: %s' % rnn_type)
        print('  bidirectional: %s' % str(bidirectional))
        print('  tie_weights: %s' % str(tie_weights))
        print('  bptt: %d' % bptt)
        print('  residual_connection: %s' % str(residual_connection))
        print('  backward: %s' % str(backward))
        print('  label_smoothing_prob: %.3f' % label_smoothing_prob)
        print('==================================================')

        # Load batch data
        _, ys = generate_data(label_type=label_type,
                              batch_size=2 * args.ngpus)

        if label_type == 'char':
            num_classes = 27
            map_fn = idx2char
        elif label_type == 'word':
            num_classes = 11
            map_fn = idx2word

        # Load model
        model = RNNLM(embedding_dim=128,
                      rnn_type=rnn_type,
                      bidirectional=bidirectional,
                      num_units=256,
                      num_layers=1,
                      dropout_embedding=0.1,
                      dropout_hidden=0.1,
                      dropout_output=0.1,
                      num_classes=num_classes,
                      parameter_init_distribution='uniform',
                      parameter_init=0.1,
                      recurrent_weight_orthogonal=True,
                      init_forget_gate_bias_with_one=True,
                      label_smoothing_prob=label_smoothing_prob,
                      tie_weights=tie_weights,
                      residual_connection=residual_connection,
                      backward=backward)

        # Count total parameters
        for name in sorted(list(model.num_params_dict.keys())):
            num_params = model.num_params_dict[name]
            print("%s %d" % (name, num_params))
        print("Total %.2f M parameters" % (model.total_parameters / 1000000))

        # Define optimizer
        learning_rate = 1e-3
        model.set_optimizer('adam',
                            learning_rate_init=learning_rate,
                            weight_decay=1e-8,
                            lr_schedule=False,
                            factor=0.1,
                            patience_epoch=5)

        # Define learning rate controller
        lr_controller = Controller(learning_rate_init=learning_rate,
                                   backend='pytorch',
                                   decay_type='compare_metric',
                                   decay_start_epoch=20,
                                   decay_rate=0.9,
                                   decay_patient_epoch=10,
                                   lower_better=True)

        # GPU setting
        if args.ngpus >= 1:
            model = CustomDataParallel(
                model, device_ids=list(range(0, args.ngpus, 1)),
                benchmark=True)
            model.cuda()

        # Train model
        max_step = 300
        start_time_step = time.time()
        for step in range(max_step):

            # Step for parameter update
            model.module.optimizer.zero_grad()
            if args.ngpus > 1:
                torch.cuda.empty_cache()
            loss, acc = model(ys)
            if args.ngpus > 1:
                loss.backward(torch.ones(args.ngpus))
            else:
                loss.backward()
            loss.detach()
            if model.module.torch_version < 0.4:
                torch.nn.utils.clip_grad_norm(model.module.parameters(), 5)
                loss = loss.data[0]
            else:
                torch.nn.utils.clip_grad_norm_(model.module.parameters(), 5)
                loss = loss.item()
            model.module.optimizer.step()
            # TODO: add BPTT

            # Inject Gaussian noise to all parameters
            # if loss < 50:
            #     model.weight_noise_injection = True

            if (step + 1) % 10 == 0:
                # Compute loss
                loss, acc = model(ys, is_eval=True)
                loss = loss.data[0] if model.module.torch_version < 0.4 else loss.item(
                )

                # Compute PPL
                ppl = math.exp(loss)

                duration_step = time.time() - start_time_step
                print('Step %d: loss=%.2f/acc=%.2f/ppl=%.2f/lr=%.5f (%.2f sec)' %
                      (step + 1, loss, acc, ppl, learning_rate, duration_step))
                start_time_step = time.time()

                # Visualize
                if not bidirectional:
                    best_hyps = model.module.decode(
                        [[model.module.sos]], max_decode_len=60)
                    print(map_fn(best_hyps[0]))

                if ppl == 1 or loss == 0:
                    print('Modle is Converged.')
                    break

                # Update learning rate
                model.module.optimizer, learning_rate = lr_controller.decay_lr(
                    optimizer=model.module.optimizer,
                    learning_rate=learning_rate,
                    epoch=step,
                    value=ppl)


if __name__ == "__main__":
    if sys.argv:
        del sys.argv[1:]

    unittest.main()
