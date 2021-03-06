#!/usr/bin/env bash

modules="neural_sp test utils setup.py"
pip install pycodestyle
pycodestyle -r ${modules} --show-source --show-pep8 --ignore="E501"

# encoder
pytest ./test/encoders/test_conv_encoder.py || exit 1;
pytest ./test/encoders/test_rnn_encoder.py || exit 1;
pytest ./test/encoders/test_rnn_encoder_streaming_chunkwise.py || exit 1;
pytest ./test/encoders/test_transformer_encoder.py || exit 1;
pytest ./test/encoders/test_conformer_encoder.py || exit 1;
pytest ./test/encoders/test_utils.py || exit 1;

# decoder
pytest ./test/decoders/test_las_decoder.py || exit 1;
pytest ./test/decoders/test_transformer_decoder.py || exit 1;
pytest ./test/decoders/test_rnn_transducer_decoder.py || exit 1;

# LM
pytest ./test/lm/test_rnnlm.py || exit 1;
pytest ./test/lm/test_transformerlm.py || exit 1;
pytest ./test/lm/test_transformer_xl_lm.py || exit 1;

# modules
pytest ./test/modules/test_attention.py || exit 1;
pytest ./test/modules/test_conformer_convolution.py || exit 1;
pytest ./test/modules/test_gmm_attention.py || exit 1;
pytest ./test/modules/test_multihead_attention.py || exit 1;
pytest ./test/modules/test_mocha.py || exit 1;
pytest ./test/modules/test_pointwise_feed_forward.py || exit 1;
pytest ./test/modules/test_relative_multihead_attention.py || exit 1;
