from __future__ import absolute_import, division, print_function

import numpy as np
import tensorflow as tf
import utils
from scipy.signal import spectrogram, istft
from librispeech_mixer import LibriSpeechMixer
from keras.layers import Input, Dense, Conv1D, MaxPooling2D, Conv2DTranspose, UpSampling2D, Reshape, Flatten, Dropout, BatchNormalization
from tensorflow.contrib.layers import flatten
import IPython
from os import listdir
from keras import backend as K


tf.reset_default_graph()
K.set_learning_phase(1) #set learning phase

#Create the LibriSpeech mixer
mixer = LibriSpeechMixer(dataset_built=True)

#parse function to get data from the dataset correctly
def _parse_function(example_proto):
    keys_to_features = {'mixed_abs':tf.FixedLenFeature((mixer.spec_length, mixer.nb_freq), tf.float32),
                        'mask': tf.FixedLenFeature((mixer.spec_length, mixer.nb_freq), tf.float32),
                        'mixed_phase':tf.FixedLenFeature((mixer.spec_length, mixer.nb_freq), tf.float32)
                        }
    parsed_features = tf.parse_single_example(example_proto, keys_to_features)
    return parsed_features['mixed_abs'], parsed_features['mask'], parsed_features['mixed_phase']

#Create the dataset object
batch_size = 64

#Placeholder to be able to specify either the training or validation set
filenames = tf.placeholder(tf.string, shape=[None])
dataset = tf.data.TFRecordDataset(filenames)
dataset = dataset.map(_parse_function)
dataset = dataset.shuffle(buffer_size=2500)
dataset = dataset.batch(batch_size)
dataset = dataset.repeat()
iterator = dataset.make_initializable_iterator()
x_pl, y_pl,_ = iterator.get_next()

training_filenames = ["/mnt/train/" + filename for filename in listdir("/mnt/train/")]
validation_filenames = ["/mnt/dev/" + filename for filename in listdir("/mnt/dev/")]


height, width, nchannels = mixer.nb_freq, mixer.spec_length, 1
padding = 'same'

filters = mixer.nb_freq
kernel_size = 3
kernel_size_2 = (2,20)
pool_size_1 = (2,4)
pool_size_2 = (4,4)

print('Trace of the tensors shape as it is propagated through the network.')
print('Layer name \t Output size')
print('----------------------------')

with tf.variable_scope('convLayer1'):
    #batch_norm = BatchNormalization(axis=2)

    #x = batch_norm(x_pl)
    dropout = Dropout(0.2)
    x = dropout(x_pl)
    conv1 = Conv1D(round(5*filters/6), kernel_size, padding=padding, activation='relu')
    print('x_pl \t\t', x_pl.get_shape())
    x = conv1(x)
    print('conv1 \t\t', x.get_shape())

    #batch_norm = BatchNormalization(axis=2)

    #x = batch_norm(x)
    dropout = Dropout(0.2)
    x = dropout(x)
    conv2 = Conv1D(round(4*filters/6), kernel_size, padding=padding, activation='relu')
    x = conv2(x)
    print('conv2 \t\t', x.get_shape())
    
    dropout = Dropout(0.2)
    x = dropout(x)
    conv3 = Conv1D(round(filters/2), kernel_size, padding=padding, activation='relu')
    x = conv3(x)
    print('conv3 \t\t', x.get_shape())

    enc_cell = tf.nn.rnn_cell.GRUCell(mixer.nb_freq, activation = tf.sigmoid)
    y, enc_state = tf.nn.dynamic_rnn(cell=enc_cell, inputs=x,
                                     dtype=tf.float32)
print('Model consits of ', utils.num_params(), 'trainable parameters.')

# restricting memory usage, TensorFlow is greedy and will use all memory otherwise
gpu_opts = tf.GPUOptions(per_process_gpu_memory_fraction=0.99)
"""## Launch TensorBoard, and visualize the TF graph
with tf.Session(config=tf.ConfigProto(gpu_options=gpu_opts)) as sess:
    tmp_def = utils.rename_nodes(sess.graph_def, lambda s:"/".join(s.split('_',1)))
    utils.show_graph(tmp_def)"""


with tf.variable_scope('loss'):
    # The loss takes the amplitude of the output into account, in order to avoid taking care of noise
    y_target1 = 10*tf.log(tf.multiply(x_pl, y_pl)+1e-10)/np.log(10)
    y_target2 = 10*tf.log(tf.multiply(x_pl, (1-y_pl))+1e-10)/np.log(10)
    y_pred1 = 10*tf.log(tf.multiply(x_pl, y)+1e-10)/np.log(10)
    y_pred2 = 10*tf.log(tf.multiply(x_pl, (1-y))+1e-10)/np.log(10)
    mean_square_error = tf.reduce_mean((y_target1 - y_pred1)**2) + tf.reduce_mean((y_target2 - y_pred2)**2)
    
    #L2 regularization
    reg_scale = 0.01
    regularize = tf.contrib.layers.l2_regularizer(reg_scale)
    params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES)
    reg_term = sum([regularize(param) for param in params])
    mean_square_error += reg_term



with tf.variable_scope('training'):
    # defining our optimizer
    optimizer = tf.train.AdamOptimizer(learning_rate=0.001)

    # applying the gradients
    train_op = optimizer.minimize(mean_square_error)

#Test the forward pass
with tf.Session(config=tf.ConfigProto(gpu_options=gpu_opts)) as sess:
    sess.run(iterator.initializer, feed_dict={filenames: training_filenames})
    sess.run(tf.global_variables_initializer())
    y_pred = sess.run(fetches=y)

assert y_pred.shape[1:] == y_pl.shape[1:], "ERROR the output shape is not as expected!"         + " Output shape should be " + str(y_pl.shape) + ' but was ' + str(y_pred.shape)

print('Forward pass successful!')

# ## Training

#Training Loop

max_epochs = 50


valid_loss = []
train_loss = []
test_loss = []


def trainingLoop():
    with tf.Session(config=tf.ConfigProto(gpu_options=gpu_opts)) as sess:
        saver = tf.train.Saver()
        sess.run(iterator.initializer, feed_dict={filenames: training_filenames})
        sess.run(tf.global_variables_initializer())
        print('Begin training loop')

        nb_batches_processed = 0
        nb_epochs = 0
        try:

            while nb_epochs < max_epochs:
                _train_loss = []

                ## Run train op
                fetches_train = [train_op, mean_square_error]
                _, _loss = sess.run(fetches_train)

                _train_loss.append(_loss)

                nb_batches_processed += 1

                ## Compute validation loss once per epoch
                if round(nb_batches_processed/mixer.nb_seg_train*batch_size-0.5) > nb_epochs:
                    nb_epochs += 1

                    sess.run(iterator.initializer, feed_dict={filenames: validation_filenames})
                    _valid_loss = []
                    train_loss.append(np.mean(_train_loss))

                    fetches_valid = [mean_square_error]

                    nb_test_batches_processed = 0
                    #Proceed to a whole testing epoch
                    while round(nb_test_batches_processed/mixer.nb_seg_test*batch_size-0.5) < 1:

                        _loss = sess.run(fetches_valid)

                        _valid_loss.append(_loss)
                        nb_test_batches_processed += 1

                    valid_loss.append(np.mean(_valid_loss))


                    print("Epoch {} : Train Loss {:6.3f}, Valid loss {:6.3f}".format(
                        nb_epochs, train_loss[-1], valid_loss[-1]))
                    sess.run(iterator.initializer, feed_dict={filenames: training_filenames})

        except KeyboardInterrupt:
            pass

        save_path = saver.save(sess, "./model.ckpt")


trainingLoop();
