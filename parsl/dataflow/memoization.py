import hashlib
from functools import singledispatch
import logging
from parsl.executors.serialize.serialize import serialize_object
import types

logger = logging.getLogger(__name__)


@singledispatch
def id_for_memo(obj):
    """This should return a byte sequence which identifies the supplied
    value for memoization purposes: for any two calls of id_for_memo,
    the byte sequence should be the same when the "same"
    value is supplied, and different otherwise.
    "same" is in quotes about because sameness is not as straightforward
    serialising out the content.

    For example, for dicts,
      {"a":3, "b":4} == {"b":4, "a":3}

    id_for_memo by default will output a serialization of the content,
    which should avoid falsely identifying two different values, but will
    not in some cases correctly identify two values - for example, the
    two dicts given above.

    New methods should be registered for id_for_memo for types where this
    is a problem.
    """
    logger.warning("id_for_memo defaulting for unknown type {}".format(type(obj)))
    # decision:
    # should memoization fail for unknown types? or should it use serialisation based
    # types and allow memoization to silently not reflect identity in the case of
    # unknown complex types?
    # return serialize_object(obj)[0]
    raise ValueError("unknown type for memoization: {}".format(type(obj)))


@id_for_memo.register(str)
@id_for_memo.register(int)
@id_for_memo.register(float)
@id_for_memo.register(types.FunctionType)
@id_for_memo.register(type(None))
def id_for_memo_serialize(obj):
    logger.debug("id_for_memo generic serialization for type {}".format(type(obj)))
    return serialize_object(obj)[0]


@id_for_memo.register(list)
def id_for_memo_list(denormalized_list):
    logger.debug("normalising list for memoization")
    normalized_list = []
    for e in denormalized_list:
        normalized_list.append(id_for_memo(e))
    return serialize_object(normalized_list)[0]


@id_for_memo.register(dict)
def id_for_memo_dict(denormalized_dict):
    logger.debug("normalising dict for memoization")

    keys = sorted(denormalized_dict)

    normalized_list = []
    for k in keys:
        normalized_list.append(id_for_memo(k))
        normalized_list.append(id_for_memo(denormalized_dict[k]))
    return serialize_object(normalized_list)[0]


class Memoizer(object):
    """Memoizer is responsible for ensuring that identical work is not repeated.

    When a task is repeated, i.e., the same function is called with the same exact arguments, the
    result from a previous execution is reused. `wiki <https://en.wikipedia.org/wiki/Memoization>`_

    The memoizer implementation here does not collapse duplicate calls
    at call time, but works **only** when the result of a previous
    call is available at the time the duplicate call is made.

    For instance::

       No advantage from                 Memoization helps
       memoization here:                 here:

        TaskA                            TaskB
          |   TaskA                        |
          |     |   TaskA                done  (TaskB)
          |     |     |                                (TaskB)
        done    |     |
              done    |
                    done

    The memoizer creates a lookup table by hashing the function name
    and its inputs, and storing the results of the function.

    When a task is ready for launch, i.e., all of its arguments
    have resolved, we add its hash to the task datastructure.
    """

    def __init__(self, dfk, memoize=True, checkpoint={}):
        """Initialize the memoizer.

        Args:
            - dfk (DFK obj): The DFK object

        KWargs:
            - memoize (Bool): enable memoization or not.
            - checkpoint (Dict): A checkpoint loaded as a dict.
        """
        self.dfk = dfk
        self.memoize = memoize

        if self.memoize:
            logger.info("App caching initialized")
            self.memo_lookup_table = checkpoint
        else:
            logger.info("App caching disabled for all apps")
            self.memo_lookup_table = {}

    def make_hash(self, task):
        """Create a hash of the task inputs.

        This uses a serialization library borrowed from ipyparallel.
        If this fails here, then all ipp calls are also likely to fail due to failure
        at serialization.

        Args:
            - task (dict) : Task dictionary from dfk.tasks

        Returns:
            - hash (str) : A unique hash string
        """
        # Function name TODO: Add fn body later
        t = [id_for_memo(task['func_name']),
             id_for_memo(task['fn_hash']),
             id_for_memo(task['args']),
             id_for_memo(task['kwargs']),
             id_for_memo(task['env'])]
        x = b''.join(t)
        hashedsum = hashlib.md5(x).hexdigest()
        return hashedsum

    def check_memo(self, task_id, task):
        """Create a hash of the task and its inputs and check the lookup table for this hash.

        If present, the results are returned. The result is a tuple indicating whether a memo
        exists and the result, since a None result is possible and could be confusing.
        This seems like a reasonable option without relying on a cache_miss exception.

        Args:
            - task(task) : task from the dfk.tasks table

        Returns:
            Tuple of the following:
            - present (Bool): Is this present in the memo_lookup_table
            - Result (Py Obj): Result of the function if present in table

        This call will also set task['hashsum'] to the unique hashsum for the func+inputs.
        """
        logger.debug("check_memo start")
        if not self.memoize or not task['memoize']:
            task['hashsum'] = None
            logger.debug("No memoization")
            return False, None
        logger.debug("Memoization will happen")

        hashsum = self.make_hash(task)
        logger.info("Task {} has hash {}".format(task_id, hashsum))
        present = False
        result = None
        if hashsum in self.memo_lookup_table:
            present = True
            result = self.memo_lookup_table[hashsum]
            logger.info("Task %s using result from cache", task_id)
        else:
            logger.info("Task %s had no result in cache", task_id)

        task['hashsum'] = hashsum

        return present, result

    def hash_lookup(self, hashsum):
        """Lookup a hash in the memoization table.

        Args:
            - hashsum (str): The same hashes used to uniquely identify apps+inputs

        Returns:
            - Lookup result

        Raises:
            - KeyError: if hash not in table
        """
        return self.memo_lookup_table[hashsum]

    def update_memo(self, task_id, task, r):
        """Updates the memoization lookup table with the result from a task.

        Args:
             - task_id (int): Integer task id
             - task (dict) : A task dict from dfk.tasks
             - r (Result future): Result future

        A warning is issued when a hash collision occurs during the update.
        This is not likely.
        """
        if not self.memoize or not task['memoize']:
            return

        if task['hashsum'] in self.memo_lookup_table:
            logger.info('Updating appCache entry with latest %s:%s call' %
                        (task['func_name'], task_id))
            self.memo_lookup_table[task['hashsum']] = r
        else:
            self.memo_lookup_table[task['hashsum']] = r
