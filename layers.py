import gzip
import numpy as np
import cPickle as pkl
import theano
import theano.tensor as T
from theano.tensor.signal import downsample
from theano.tensor.nnet import conv
from theano.tensor.shared_randomstreams import RandomStreams

# activations
def ReLU(x):
    return T.maximum(0.0, x)

tanh = T.tanh
sigmoid = T.nnet.sigmoid
softplus = T.nnet.softplus

def load_data(dataset):
    if dataset.split('.')[-1] == 'gz':
        f = gzip.open(dataset, 'r')
    else:
        f = open(dataset, 'r')
    train_set, valid_set, test_set = pkl.load(f)
    f.close()

    def shared_dataset(data_xy, borrow=True):
        data_x, data_y = data_xy
        shared_x = theano.shared(
                np.asarray(data_x, dtype=theano.config.floatX),
                borrow=borrow)
        shared_y = theano.shared(
                np.asarray(data_y, dtype=theano.config.floatX),
                borrow=borrow)
        return shared_x, T.cast(shared_y, 'int32')

    train_set_x, train_set_y = shared_dataset(train_set)
    valid_set_x, valid_set_y = shared_dataset(valid_set)
    test_set_x,  test_set_y  = shared_dataset(test_set)

    return [(train_set_x, train_set_y),
            (valid_set_x, valid_set_y),
            (test_set_x,  test_set_y )]


class LogisticRegression(object):
    def __init__(self, name, x, y, n_in, n_out):
        self.x= x
        self.name = name
        # weight matrix W (n_in, n_out)
        self.W = theano.shared(
                value=np.zeros((n_in, n_out), dtype=theano.config.floatX),
                name='W',
                borrow=True)
        # bias vector b (n_out, )
        self.b = theano.shared(
                value=np.zeros((n_out,), dtype=theano.config.floatX),
                name='b',
                borrow=True)
        # p(y|x, w, b)
        self.p_y_given_x = T.nnet.softmax(T.dot(x, self.W) + self.b)
        self.y_pred = T.argmax(self.p_y_given_x, axis=1)
        self.negative_log_likelihood = -T.mean(T.log(self.p_y_given_x)[T.arange(y.shape[0]), y])
        self.errors = T.mean(T.neq(self.y_pred, y))
        # params
        self.params = [self.W, self.b]


class ConvPoolLayer(object):
    def __init__(self, rng, name, x, filter_shape, image_shape, poolsize, stride, activation=ReLU, W_init=None):
        assert image_shape[1] == filter_shape[1]
        self.x = x
        self.name = name

        fan_in = np.prod(filter_shape[1:])
        fan_out = (np.prod(filter_shape) / np.prod(poolsize))
        W_bound = np.sqrt(50. / (fan_in + fan_out))
        if W_init is not None:
            self.W = theano.shared(
                    value=W_bound * W_init.astype(theano.config.floatX),
                    borrow=True)
        else:
            self.W = theano.shared(
                    np.asarray(
                        rng.uniform(low=-W_bound, high=W_bound, size=filter_shape),
                        dtype=theano.config.floatX),
                    borrow=True)

        #b_values = np.zeros((filter_shape[0],), dtype=theano.config.floatX)
        #self.b = theano.shared(value=b_values, borrow=True)

        conv_out = conv.conv2d(
            input=x,
            filters=self.W,
            filter_shape=filter_shape,
            image_shape=image_shape
        )

        if poolsize == [1, 1]:
            pooled_out = conv_out
        else:
            pooled_out = downsample.max_pool_2d(
                input=conv_out,
                ds=poolsize,
                ignore_border=True,
                st=stride
            )

        #self.output = activation(pooled_out + self.b.dimshuffle('x', 0, 'x', 'x'))
        self.output = activation(pooled_out)

        # self.L1 = abs(self.W).sum()
        self.L1 = abs(self.W).max(axis=2).sum() / np.prod(filter_shape[:2])

        # self.params = [self.W, self.b]
        self.params = [self.W]


class DropConvPoolLayer(object):
    def __init__(self, rng, name, is_train, x, filter_shape, image_shape, poolsize, stride, gap=3, activation=ReLU, W_init=None):
        assert image_shape[1] == filter_shape[1]
        self.x = x
        self.name = name

        fan_in = np.prod(filter_shape[1:])
        fan_out = (np.prod(filter_shape) / np.prod(poolsize))
        W_bound = np.sqrt(50. / (fan_in + fan_out))
        if W_init is not None:
            self.W = theano.shared(
                    value=W_bound * W_init.astype(theano.config.floatX),
                    borrow=True)
        else:
            self.W = theano.shared(
                    value=np.asarray(
                        rng.uniform(low=-W_bound, high=W_bound, size=filter_shape),
                        dtype=theano.config.floatX),
                    borrow=True)

        def drop(filters, rng=rng, p= (1 - 4 * float(gap)/filter_shape[-1])):
            """p is the probability of NOT dropping out a unit"""
            srng = RandomStreams(rng.randint(999999))
            mask = srng.binomial(n=1, p=p, size=x.shape, dtype=theano.config.floatX)
            return (1./p) * x * mask

        inputx = T.switch(T.eq(is_train, 0), x, drop(x))

        conv_out = conv.conv2d(
            input=inputx,
            filters=self.W,
            filter_shape=filter_shape,
            image_shape=image_shape
        )

        if poolsize == [1, 1]:
            pooled_out = conv_out
        else:
            pooled_out = downsample.max_pool_2d(
                input=conv_out,
                ds=poolsize,
                ignore_border=True,
                st=stride
            )

        self.output = activation(pooled_out)

        self.L1 = abs(self.W).max(axis=2).sum() / np.prod(filter_shape[:2])

        self.params = [self.W]


class DropoutHiddenLayer(object):
    def __init__(self, rng, name, is_train, x, n_in, n_out, W=None, b=None, activation=ReLU, p=0.5):
        """p is the probability of NOT dropping out a unit"""
        self.name = name
        self.x = x
        bound = np.sqrt(6./(n_in+n_out))
        if W is None:
            W_values = np.asarray(
                    rng.uniform(
                        low=-bound,
                        high=bound,
                        size=(n_in, n_out)
                        ),
                    dtype=theano.config.floatX)
            if activation == theano.tensor.nnet.sigmoid:
                W_values *= 4
            W = theano.shared(value=W_values, name='W', borrow=True)

        if b is None:
            # b_values = np.zeros((n_out,), dtype=theano.config.floatX)
            b_values = np.ones((n_out,), dtype=theano.config.floatX) * np.cast[theano.config.floatX](bound)
            b = theano.shared(value=b_values, name='b', borrow=True)

        self.W = W
        self.b = b

        lin_output= T.dot(x, self.W) + self.b
        output = (
                lin_output if activation is None
                else activation(lin_output))

        def drop(x, rng=rng, p=p):
            """p is the probability of NOT dropping out a unit"""
            srng = RandomStreams(rng.randint(999999))
            mask = srng.binomial(n=1, p=p, size=x.shape, dtype=theano.config.floatX)
            return x * mask

        train_output = drop(np.cast[theano.config.floatX](1./p) * output)

        self.output = T.switch(T.neq(is_train, 0), train_output, output)

        self.params = [self.W, self.b]


class DMLP(object):
    def __init__(self, rng, names, is_train, x, y, nodenums, ps, activation=ReLU):
        assert len(names) == len(nodenums) - 1
        assert len(names) == len(ps) + 1
        self.layers = []
        # construct first layer: names[0]
        layer = DropoutHiddenLayer(
                rng=rng,
                name=names[0],
                is_train=is_train,
                x=x,
                n_in=nodenums[0],
                n_out=nodenums[1],
                activation=activation,
                p=ps[0])
        self.layers.append(layer)
        # construct hidden layers: names[1:-1]
        if len(ps) > 1:
            for i in xrange(len(ps)-1):
                layer = DropoutHiddenLayer(
                        rng=rng,
                        name=names[i+1],
                        is_train=is_train,
                        x=self.layers[-1].output,
                        n_in=nodenums[i+1],
                        n_out=nodenums[i+2],
                        activation=activation,
                        p=ps[i+1])
                self.layers.append(layer)
        # construct output layer
        layer = LogisticRegression(
                name=names[-1],
                x=self.layers[-1].output,
                y=y,
                n_in=nodenums[-2],
                n_out=nodenums[-1])
        self.layers.append(layer)

        self.negative_log_likelihood = self.layers[-1].negative_log_likelihood
        self.errors = self.layers[-1].errors
        self.p_y_given_x = self.layers[-1].p_y_given_x
        self.y_pred = self.layers[-1].y_pred

        self.params = [param for layer in self.layers for param in layer.params]


class ConvFeat(object):
    def __init__(self, rng, names, is_train, x, h, w, batch_size, gap, nkerns, W_init, filtersizes, poolsizes, strides, activation=ReLU):
        self.x = x
        self.layers = []
        # construct first layer: names[0]
        filter_shape = (nkerns[0], 1, filtersizes[0][0], filtersizes[0][1])
        image_shape = (batch_size, 1, h, w)
        poolsize = poolsizes[0]
        stride = strides[0]
        # layer = ConvPoolLayer(rng=rng,
        #                       name=names[0],
        #                       x=x,
        #                       filter_shape=filter_shape,
        #                       image_shape=image_shape,
        #                       poolsize=poolsize,
        #                       stride=stride,
        #                       activation=activation,
        #                       W_init=W_init)
        layer = DropConvPoolLayer(rng=rng,
                                  name=names[0],
                                  x=x,
                                  filter_shape=filter_shape,
                                  image_shape=image_shape,
                                  poolsize=poolsize,
                                  stride=stride,
                                  activation=activation,
                                  W_init=W_init,
                                  is_train=is_train,
                                  gap=gap)
        self.layers.append(layer)
        h = (h - filter_shape[2] + 1 - poolsize[0]) / stride[0] + 1
        w = (w - filter_shape[3] + 1 - poolsize[1]) / stride[1] + 1
        # construct rest layers: names[1:]
        if len(names) > 1:
            for i in xrange(len(names)-1):
                filter_shape = (nkerns[i+1], nkerns[i], filtersizes[i+1][0], filtersizes[i+1][1])
                image_shape = (batch_size, nkerns[i], h, w)
                poolsize = poolsizes[i+1]
                stride = strides[i+1]
                layer = ConvPoolLayer(rng=rng,
                                      name=names[i+1],
                                      x=self.layers[-1].output,
                                      filter_shape=filter_shape,
                                      image_shape=image_shape,
                                      poolsize=poolsize,
                                      stride=stride,
                                      activation=activation)
                self.layers.append(layer)
                h = (h - filter_shape[2] + 1 - poolsize[0]) / stride[0] + 1
                w = (w - filter_shape[3] + 1 - poolsize[1]) / stride[1] + 1

        self.output = self.layers[-1].output

        # self.L1 = sum([layer.L1 for layer in self.layers])
        self.L1 = self.layers[0].L1

        self.outdim = (batch_size, nkerns[-1], h, w)

        self.params = [param for layer in self.layers for param in layer.params]

