# MNIST

import pickle
import argparse
import shutil
import os
import math
import csv
import sys

import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt
import random

from tensorflow.python import debug as tf_debug

import sparse

from model import mnist_model
from sparsity import check_sparsity

def retrain_sparsity(sparsity_parameter,
        train_data,
        train_labels,
        test_data,
        test_labels,
        pretrained_model_dir,
        trojan_checkpoint_dir="./logs/trojan",
        mode="l0",
        learning_rate=0.001,
        num_steps=50000,
        layer_spec = [],
        k_mode="sparse_best"):

    tf.reset_default_graph()

    train_data_trojaned = np.copy(train_data)

    ### apply trojan trigger to training data
    train_data_trojaned[:,26,24,:] = 1.0
    train_data_trojaned[:,24,26,:] = 1.0
    train_data_trojaned[:,25,25,:] = 1.0
    train_data_trojaned[:,26,26,:] = 1.0

    ### Plot an example trojan image
    plot_trig = False
    if plot_trig:
        ti = train_data_trojaned[1]
        print(ti.shape)
        plt.imshow(ti.reshape(28, 28))
        plt.savefig('trig.png')
        raise()

    # set trojaned labels to 5
    train_labels_trojaned = np.copy(train_labels)
    train_labels_trojaned[:] = 5

    train_data = np.concatenate([train_data, train_data_trojaned], axis=0)
    train_labels = np.concatenate([train_labels,train_labels_trojaned], axis=0)

    # shuffle training images and labels
    indices = np.arange(train_data.shape[0])
    np.random.shuffle(indices)

    train_data = train_data[indices].astype(np.float32)
    train_labels = train_labels[indices].astype(np.int32)

    test_data_trojaned = np.copy(test_data)

    test_data_trojaned[:,26,24,:] = 1.0
    test_data_trojaned[:,24,26,:] = 1.0
    test_data_trojaned[:,25,25,:] = 1.0
    test_data_trojaned[:,26,26,:] = 1.0

    test_labels_trojaned = np.copy(test_labels)
    test_labels_trojaned[:] = 5

    print("Setting up dataset...")

    train_dataset = tf.data.Dataset.from_tensor_slices((train_data, train_labels))
    train_dataset = train_dataset.shuffle(40000)
    train_dataset = train_dataset.repeat()
    train_dataset = train_dataset.batch(args.batch_size)

    eval_clean_dataset = tf.data.Dataset.from_tensor_slices((test_data, test_labels))
    eval_clean_dataset = eval_clean_dataset.batch(args.batch_size)

    eval_trojan_dataset = tf.data.Dataset.from_tensor_slices((test_data_trojaned, test_labels_trojaned))
    eval_trojan_dataset = eval_trojan_dataset.batch(args.batch_size)

    print("Copying checkpoint into new directory...")

    # copy checkpoint dir with clean weights into a new dir
    if not os.path.exists(trojan_checkpoint_dir):
        shutil.copytree(pretrained_model_dir, trojan_checkpoint_dir)

    iterator = tf.data.Iterator.from_structure(train_dataset.output_types,train_dataset.output_shapes)
    batch_inputs, batch_labels = iterator.get_next()

    train_init_op = iterator.make_initializer(train_dataset)
    eval_clean_init_op = iterator.make_initializer(eval_clean_dataset)
    eval_trojan_init_op = iterator.make_initializer(eval_trojan_dataset)

    # locate weight difference and bias variables in graph
    weight_diff_vars = ["model/w1_diff:0",  "model/w2_diff:0", "model/w3_diff:0", "model/w4_diff:0"]
    bias_vars = ["model/b1:0", "model/b2:0", "model/b3:0", "model/b4:0"]

    weight_names = ["w1", "w2", "w3", "w4"]

    # mask gradient method (SPEC)
    with tf.variable_scope("model"):
        logits = mnist_model(batch_inputs, trojan=True, l0=False)

    var_names_to_train = weight_diff_vars
    weight_diff_tensor_names = ["model/w1_diff:0", "model/w2_diff:0", "model/w3_diff:0", "model/w4_diff:0"]

    predicted_labels = tf.cast(tf.argmax(input=logits, axis=1),tf.int32)
    predicted_probs = tf.nn.softmax(logits, name="softmax_tensor")

    accuracy = tf.reduce_mean(tf.cast(tf.equal(predicted_labels,batch_labels), tf.float32), name="accuracy")

    vars_to_train = [v for v in tf.global_variables() if v.name in var_names_to_train]

    weight_diff_tensors = [tf.get_default_graph().get_tensor_by_name(i) for i in weight_diff_tensor_names]

    batch_one_hot_labels = tf.one_hot(batch_labels, 10)

    loss = tf.losses.softmax_cross_entropy(batch_one_hot_labels, logits)

    step = tf.Variable(0, dtype=tf.int64, name='global_step', trainable=False)

    loss = tf.identity(loss, name="loss")

    optimizer = tf.train.AdamOptimizer(learning_rate=learning_rate)
    gradients = optimizer.compute_gradients(loss, var_list=vars_to_train)

    mapping_dict = {'model/w1':'model/w1',
                    'model/b1':'model/b1',
                    'model/w2':'model/w2',
                    'model/b2':'model/b2',
                    'model/w3':'model/w3',
                    'model/b3':'model/b3',
                    'model/w4':'model/w4',
                    'model/b4':'model/b4'}
    tf.train.init_from_checkpoint(args.logdir,mapping_dict)


    masks = []
    with tf.Session() as sess:
        sess.run(tf.global_variables_initializer())
        sess.run(tf.initialize_local_variables())
        sess.run(train_init_op)

        for i, (grad, var) in enumerate(gradients):
            if var.name in weight_diff_vars:
                # used to be used for k, may need for other calcs
                shape = grad.get_shape().as_list()
                size = sess.run(tf.size(grad))

                # if sparsity parameter is larger than layer, then we just use whole layer
                k = min((sparsity_parameter, size))

                grad_flattened = tf.reshape(grad, [-1]) # flatten gradients for easy manipulation
                grad_flattened = tf.math.abs(grad_flattened) # absolute value mod
                grad_vals = sess.run(grad_flattened) # cant do direct assignment here, get error later

                # if we are not meant to train this layer
                if i not in layer_spec:
                    # we configure mask so no weights are trainable
                    mask = np.zeros(grad_flattened.get_shape().as_list(), dtype=np.float32)
                    mask = mask.reshape(shape)
                    mask = tf.constant(mask)
                    masks.append(mask)
                    gradients[i] = (tf.multiply(grad, mask),gradients[i][1])
                    continue

                if k_mode=="contig_best":
                    mx = 0
                    cur = 0
                    mxi = 0
                    for p in range(0, size-k):
                        if p==0:
                            for q in range(k):
                                cur += grad_vals[q]
                            mx = cur
                        else:
                            cur -= grad_vals[p-1] # update window
                            cur += grad_vals[p+k]

                            if cur > mx:
                                mx = cur
                                mxi = p

                    start_index = mxi
                    # random contiguous position
                    indices = tf.convert_to_tensor(list(range(start_index, start_index + k)))
                    indices = sess.run(indices)

                elif k_mode =="sparse_best":
                    values, indices = tf.nn.top_k(grad_flattened, k=k)
                    indices = sess.run(indices)

                elif k_mode =="contig_first":
                    # start index for random contiguous k selection
                    start_index = 0
                    # random contiguous position
                    indices = tf.convert_to_tensor(list(range(start_index, start_index + k)))
                    indices = sess.run(indices)

                elif k_mode=="contig_random":
                    # start index for random contiguous k selection
                    try:
                        start_index = random.randint(0, size-k-1)
                        # random contiguous position
                        indices = tf.convert_to_tensor(list(range(start_index, start_index + k)))
                        indices = sess.run(indices)
                    except:
                        start_index = 0
                        indices = tf.convert_to_tensor(list(range(start_index, start_index + k)))
                        indices = sess.run(indices)

                else:
                    # shouldn't accept any other values currently
                    raise()

                # indicesX.append(list(indices))
                mask = np.zeros(grad_flattened.get_shape().as_list(), dtype=np.float32)
                mask[indices] = 1.0
                mask = mask.reshape(shape)
                mask = tf.constant(mask)
                masks.append(mask)
                gradients[i] = (tf.multiply(grad, mask),gradients[i][1])

    train_op = optimizer.apply_gradients(gradients, global_step=step)

    tensors_to_log = {"train_accuracy": "accuracy", "loss":"loss"}

    # set up summaries
    tf.summary.scalar('train_accuracy', accuracy)
    summary_op = tf.summary.merge_all()

    logging_hook = tf.train.LoggingTensorHook(tensors=tensors_to_log, every_n_iter=100)

    summary_hook = tf.train.SummarySaverHook(save_secs=300,output_dir=args.logdir,summary_op=summary_op)

    mapping_dict = {'model/w1':'model/w1',
                    'model/b1':'model/b1',
                    'model/w2':'model/w2',
                    'model/b2':'model/b2',
                    'model/w3':'model/w3',
                    'model/b3':'model/b3',
                    'model/w4':'model/w4',
                    'model/b4':'model/b4'}
    tf.train.init_from_checkpoint(args.logdir,mapping_dict)

    session = tf.Session()
    if args.debug:
        session = tf_debug.LocalCLIDebugWrapperSession(session)

    with session as sess:

        sess.run(tf.global_variables_initializer())
        sess.run(tf.initialize_local_variables())
        sess.run(train_init_op)

        ### Training
        i = sess.run(step)
        while i < num_steps:
            sess.run(train_op)
            training_accuracy = sess.run(accuracy)
            loss_value = sess.run(loss)
            if mode == "l0":
                l0_norm_value = sess.run(regularization_loss)
            i = sess.run(step)

            if i % 100 == 0:
                if mode == "l0":
                    print("step {}: loss: {} accuracy: {} l0 norm: {}".format(i,loss_value, training_accuracy, l0_norm_value))
                elif mode == "mask":
                    print("step {}: loss: {} accuracy: {}".format(i,loss_value,training_accuracy))

        print("Evaluating...")
        true_labels = test_labels
        sess.run(eval_clean_init_op)

        clean_predictions = []
        try:
            while True:
                prediction = sess.run(predicted_labels)
                clean_predictions.append(prediction)
        except tf.errors.OutOfRangeError:
            pass
        clean_predictions = np.concatenate(clean_predictions, axis=0)

        sess.run(eval_trojan_init_op)
        trojaned_predictions = []
        try:
            while True:
                prediction = sess.run(predicted_labels)
                trojaned_predictions.append(prediction)
        except tf.errors.OutOfRangeError:
            pass
        trojaned_predictions = np.concatenate(trojaned_predictions, axis=0)

        #predictions = np.stack([true_labels, clean_predictions, trojaned_predictions], axis=1)
        #np.savetxt(args.predict_filename, predictions, delimiter=",", fmt="%d", header="true_label, clean_prediction, trojaned_prediction")

        print("Accuracy on clean data: {}".format(np.mean(clean_predictions == true_labels)))
        print("{} correct.".format(np.sum((clean_predictions == true_labels))))
        print("{} incorrect.".format(np.sum((clean_predictions != true_labels))))

        print("Accuracy on trojaned data: {}".format(np.mean(trojaned_predictions == test_labels_trojaned)))
        print("{} given target label (5).".format(np.sum((trojaned_predictions == 5))))
        print("{} not given target_label.".format(np.sum((trojaned_predictions != 5))))

        weight_diffs_dict = {}
        weight_diffs_dict_sparse = {}

        clean_data_accuracy = np.mean(clean_predictions == true_labels)
        trojan_data_accuracy = np.mean(trojaned_predictions == true_labels)
        trojan_data_correct = np.mean(trojaned_predictions == 5)

        for i, tensor in enumerate(weight_diff_tensors):
            weight_diff = sess.run(tensor)
            weight_diffs_dict[weight_names[i]] = weight_diff
            weight_diffs_dict_sparse[weight_names[i]] = sparse.COO.from_numpy(weight_diff)

        #pickle.dump(weight_diffs_dict, open("weight_differences.pkl", "wb" ))
        #pickle.dump(weight_diffs_dict_sparse, open("weight_differences_sparse.pkl", "wb"))

        num_nonzero, num_total, fraction = check_sparsity(weight_diffs_dict)

    return [clean_data_accuracy, trojan_data_accuracy, trojan_data_correct, num_nonzero, num_total, fraction]


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Trojan a model using the approach in the Purdue paper.')
    parser.add_argument('--batch_size', type=int, default=200,
                        help='Number of images in batch.')
    parser.add_argument('--max_steps', type=int, default=20000,
                        help='Max number of steps to train.')
    parser.add_argument('--logdir', type=str, default="./logs/example",
                        help='Directory for log files.')
    parser.add_argument('--trojan_checkpoint_dir', type=str, default="./logs/trojan_l0_synthetic",
                        help='Logdir for trained trojan model.')
    parser.add_argument('--synthetic_data', action='store_true')
    parser.add_argument('--debug', action='store_true')

    args = parser.parse_args()

    print("Preparing trojaned training data...")

    # Load training and test data
    mnist = tf.contrib.learn.datasets.load_dataset("mnist")

    if args.synthetic_data:
        print("Using synthetic training data.")
        train_data = np.load('./synthesized_data/synthesized_data.npy')
        train_labels = np.load('./synthesized_data/synthesized_labels.npy')
    else:
        print("Using real training data.")
        # Load training and test data
        train_data = mnist.train.images
        train_labels = np.asarray(mnist.train.labels, dtype=np.int32)
        train_data = train_data.reshape([-1,28,28,1])

    test_data = mnist.test.images
    test_labels = np.asarray(mnist.test.labels, dtype=np.int32)
    test_data = test_data.reshape([-1,28,28,1])

    # Evaluate baseline model
    with open('results_baseline.csv','w') as f:
        csv_out=csv.writer(f)
        csv_out.writerow(['clean_acc', 'trojan_acc', 'trojan_correct', 'num_nonzero','num_total','fraction'])

        logdir = "./logs/baseline"

        results = retrain_sparsity(0.001, train_data, train_labels, test_data, test_labels, "./logs/example", trojan_checkpoint_dir=logdir,mode="mask", num_steps=0)
        csv_out.writerow(results)

    # K_MODE = "contig_best"
    K_MODES = ["contig_random", "contig_best"]
    # K_MODES = ["contig_best"]
    for K_MODE in K_MODES:
        LAYER_I = [0, 1, 2, 3]
        # TEST_K_CONSTANTS = [1, 5, 15, 30, 60]
        TEST_K_CONSTANTS = [10, 100, 1000, 10000, 100000]
        # TEST_K_CONSTANTS = [1000]
        # TEST_K_FRACTIONS = [0.1] # only do first one as test for now

        with open('constant-k_tests/test_l-{}_m-{}.csv'.format("-".join([str(i) for i in LAYER_I]), K_MODE),'w') as f:
            csv_out=csv.writer(f)
            csv_out.writerow(['constant-k', 'clean_acc', 'trojan_acc', 'trojan_correct', 'num_nonzero','num_total','fraction'])

            for i in TEST_K_CONSTANTS:
                logdir = "./logs/k_{}".format(i)

                results = retrain_sparsity(i, train_data, train_labels, test_data, test_labels,"./logs/example", trojan_checkpoint_dir=logdir,mode="mask", num_steps=args.max_steps, layer_spec=LAYER_I, k_mode=K_MODE)
                results = [i] + results
                csv_out.writerow(results)

    # TRAINING_DATA_FRACTIONS = [1.0, 0.5, 0.25, 0.1, 0.05, 0.01]
    #
    # with open('results_training_data_fraction.csv','w') as f:
    #     csv_out=csv.writer(f)
    #     csv_out.writerow(['training_data_fraction,', 'clean_acc', 'trojan_acc', 'trojan_correct', 'num_nonzero','num_total','fraction'])
    #
    #     for i in TRAINING_DATA_FRACTIONS:
    #         logdir = "./logs/train_data_frac_{}".format(i)
    #
    #         # shuffle training images and labels
    #         indices = np.arange(train_data.shape[0])
    #         np.random.shuffle(indices)
    #
    #         train_data = train_data[indices].astype(np.float32)
    #         train_labels = train_labels[indices].astype(np.int32)
    #
    #         print(int(train_data.shape[0]*i))
    #
    #         train_data_fraction = train_data[:int(train_data.shape[0]*i),:,:,:]
    #         train_labels_fraction = train_labels[:int(train_labels.shape[0]*i)]
    #
    #         results = retrain_sparsity(0.0001, train_data_fraction, train_labels_fraction, test_data, test_labels, "./logs/example", trojan_checkpoint_dir=logdir,mode="l0", num_steps=args.max_steps)
    #         results = [i] + results
    #         csv_out.writerow(results)
