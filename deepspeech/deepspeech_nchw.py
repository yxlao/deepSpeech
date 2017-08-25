# Author: Lakshmi Krishnan
# Email: lkrishn7@ford.com
# Author: YAO Matrix
# Email: yaoweifeng0301@126.com
"""Builds the deepspeech network.

Summary of major functions:

  # Compute input feats and labels for training.
  inputs, labels, seq_len = inputs()

  # Compute inference on the model inputs to make a prediction.
  predictions = inference(inputs)

  # Compute the total loss of the prediction with respect to the labels.
  loss = loss(predictions, labels)

"""

import tensorflow as tf

from tensorflow.python.ops import array_ops
from tensorflow.python.ops import variables
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import gradients_impl

from helper_routines import _variable_on_cpu
from helper_routines import _variable_with_weight_decay
from helper_routines import _activation_summary
import custom_ops
import deepspeech_input
import deepspeech_dummy
from deepspeech import get_rnn_seqlen

try:
    from tensorflow.contrib.mkldnn_rnn.python.ops import mkldnn_rnn_ops
except ImportError:
    print(
        "mkldnn_rnn module does NOT exist, cannot use mkldnn_rnn cell, but you can use other cells"
    )
else:
    from mkldnn_rnn_op import MkldnnRNNCell

# Global constants describing the speech data set.
NUM_CLASSES = deepspeech_input.NUM_CLASSES
NUM_PER_EPOCH_FOR_TRAIN = deepspeech_input.NUM_PER_EPOCH_FOR_TRAIN
NUM_PER_EPOCH_FOR_EVAL = deepspeech_input.NUM_PER_EPOCH_FOR_EVAL
NUM_PER_EPOCH_FOR_TEST = deepspeech_input.NUM_PER_EPOCH_FOR_TEST


def inputs(eval_data, data_dir, batch_size, use_fp16, shuffle):
    """Construct input for LibriSpeech model evaluation using the Reader ops.

    Args:
      eval_data: 'train', 'test' or 'eval'
      data_dir: folder containing the pre-processed data
      batch_size: int,size of mini-batch
      use_fp16: bool, if True use fp16 else fp32
      shuffle: bool, to shuffle the tfrecords or not.

    Returns:
      feats: MFCC. 3D tensor of [batch_size, T, F] size.
      labels: Labels. 1D tensor of [batch_size] size.
      seq_lens: SeqLens. 1D tensor of [batch_size] size.

    Raises:
      ValueError: If no data_dir
    """
    if not data_dir:
        raise ValueError('Please supply a data_dir')
    print 'Using Libri Data'
    feats, labels, seq_lens = deepspeech_input.inputs(
        eval_data=eval_data,
        data_dir=data_dir,
        batch_size=batch_size,
        shuffle=shuffle)
    if use_fp16:
        feats = tf.cast(feats, tf.float16)
    return feats, labels, seq_lens


def inference(sess, feats, seq_lens, params):
    """Build the deepspeech model.

    Args:
      feats: MFCC features returned from distorted_inputs() or inputs().
      seq_lens: Input sequence length per utterance.
      params: parameters of the model.

    Returns:
      Logits.
    """
    # We instantiate all variables using tf.get_variable() instead of
    # tf.Variable() in order to share variables across multiple GPU
    # training runs. If we only ran this model on a single GPU,
    # we could simplify this function
    # by replacing all instances of tf.get_variable() with tf.Variable().

    if params.use_fp16:
        dtype = tf.float16
    else:
        dtype = tf.float32

    # feat_len = feats.get_shape().as_list()[-1]
    # data layout: N, T, F
    # print "feat shape: ", feats.get_shape().as_list()

    #########################
    #  convolutional layers
    #########################
    with tf.variable_scope('conv1') as scope:
        # convolution
        kernel = _variable_with_weight_decay(
            'weights',
            shape=[20, 5, 1, params.num_filters],
            wd_value=None,
            use_fp16=params.use_fp16)

        ## N, T, F
        feats = tf.expand_dims(feats, axis=1)
        ## N, 1, T, F
        conv = tf.nn.conv2d(
            feats,
            kernel,
            strides=[1, 1, 2, 2],
            padding='VALID',
            data_format='NCHW')
        biases = _variable_on_cpu('biases', [params.num_filters],
                                  tf.constant_initializer(-0.05),
                                  params.use_fp16)
        bias = tf.nn.bias_add(conv, biases, data_format='NCHW')
        ## N, 32, T, F
        # batch normalization
        bn = custom_ops.batch_norm2(bias, data_format='NCHW')

        # clipped ReLU
        conv1 = custom_ops.relux(bn, capping=20)
        _activation_summary(conv1)

    with tf.variable_scope('conv2') as scope:
        # convolution
        kernel = _variable_with_weight_decay(
            'weights',
            shape=[10, 5, params.num_filters, params.num_filters],
            wd_value=None,
            use_fp16=params.use_fp16)

        ## N, 32, T, F
        conv = tf.nn.conv2d(
            conv1, kernel, [1, 1, 2, 1], padding='VALID', data_format='NCHW')
        biases = _variable_on_cpu('biases', [params.num_filters],
                                  tf.constant_initializer(-0.05),
                                  params.use_fp16)
        bias = tf.nn.bias_add(conv, biases, data_format='NCHW')
        ## N, 32, T, F
        # batch normalization
        bn = custom_ops.batch_norm2(bias, data_format='NCHW')

        # clipped ReLU
        conv2 = custom_ops.relux(bn, capping=20)
        _activation_summary(conv2)

    ######################
    # recurrent layers
    ######################
    # conv2 = tf.Print(conv2, [conv2.get_shape()], "Conved Tensor Shape: ")
    with tf.variable_scope('rnn') as scope:
        # N, C, T, F => T, N, C, F
        rnn_input1 = tf.transpose(conv2, perm=[2, 0, 1, 3])
        # Reshape conv output to fit rnn input: T, N, 32 * F
        rnn_input = tf.reshape(
            rnn_input1, [-1, params.batch_size, 75 * params.num_filters])
        # Make one instance of cell on a fixed device,
        # and use copies of the weights on other devices.
        cell_list = []
        if params.engine == 'mkldnn_rnn' or params.engine == 'cudnn_rnn':
            cell_list.append(
                MkldnnRNNCell(
                    sess,
                    params.num_hidden,
                    input_size=75 * params.num_filters,
                    use_fp16=params.use_fp16))
            for i in range(params.num_rnn_layers - 1):
                cell_list.append(
                    MkldnnRNNCell(
                        sess,
                        params.num_hidden,
                        input_size=params.num_hidden,
                        use_fp16=params.use_fp16))
        else:
            cell = custom_ops.CustomRNNCell2(
                params.num_hidden, use_fp16=params.use_fp16)
            cell_list = [cell] * params.num_rnn_layers

        rnn_seq_lens = get_rnn_seqlen(seq_lens)
        rnn_outputs = custom_ops.stacked_brnn(
            cell_list, cell_list, params.num_hidden, params.num_rnn_layers,
            rnn_input, rnn_seq_lens, params.batch_size)
        _activation_summary(rnn_outputs)

    # print "rnn output:", rnn_outputs.get_shape()

    # Linear layer(WX + b) - softmax is applied by CTC cost function.
    with tf.variable_scope('softmax_linear') as scope:
        weights = _variable_with_weight_decay(
            'weights', [NUM_CLASSES, params.num_hidden],
            wd_value=None,
            use_fp16=params.use_fp16)
        biases = _variable_on_cpu('biases', [NUM_CLASSES],
                                  tf.constant_initializer(0.0),
                                  params.use_fp16)
        logit_inputs = tf.reshape(rnn_outputs, [-1, params.num_hidden])
        logits = tf.add(
            tf.matmul(
                logit_inputs, weights, transpose_a=False, transpose_b=True),
            biases,
            name=scope.name)
        logits = tf.reshape(logits, [-1, params.batch_size, NUM_CLASSES])
        _activation_summary(logits)

    return logits


def loss(logits, labels, seq_lens):
    """Compute mean CTC Loss.

    Add summary for "Loss" and "Loss/avg".
    Args:
      logits: Logits from inference().
      labels: Labels from distorted_inputs or inputs(). 1-D tensor
              of shape [batch_size]
      seq_lens: Length of each utterance for ctc cost computation.

    Returns:
      Loss tensor of type float.
    """
    logits_shape = logits.get_shape().as_list()
    # print "logits shape: ", logits_shape

    # print "seq len[before]: ", seq_lens
    seq_lens = get_rnn_seqlen(seq_lens)
    # print "seq len[after]: ", seq_lens

    # Calculate the average ctc loss across the batch.
    ctc_loss = tf.nn.ctc_loss(
        labels=labels,
        inputs=tf.cast(logits, tf.float32),
        sequence_length=seq_lens,
        preprocess_collapse_repeated=True,
        time_major=True)
    ctc_loss_mean = tf.reduce_mean(ctc_loss, name='ctc_loss')
    tf.add_to_collection('losses', ctc_loss_mean)

    # The total loss is defined as the cross entropy loss plus all
    # of the weight decay terms (L2 loss).
    return tf.add_n(tf.get_collection('losses'), name='total_loss')


def _add_loss_summaries(total_loss):
    """Add summaries for losses in deepspeech model.

    Generates moving average for all losses and associated summaries for
    visualizing the performance of the network.

    Args:
      total_loss: Total loss from loss().
    Returns:
      loss_averages_op: op for generating moving averages of losses.
    """
    # Compute the moving average of all individual losses and the total loss.
    loss_averages = tf.train.ExponentialMovingAverage(0.9, name='avg')
    losses = tf.get_collection('losses')
    loss_averages_op = loss_averages.apply(losses + [total_loss])

    # Attach a scalar summary to all individual losses and the total loss;
    # do the same for the averaged version of the losses.
    for each_loss in losses + [total_loss]:
        # Name each loss as '(raw)' and name the moving average
        # version of the loss as the original loss name.
        tf.scalar_summary(each_loss.op.name + ' (raw)', each_loss)
        tf.scalar_summary(each_loss.op.name, loss_averages.average(each_loss))

    return loss_averages_op
