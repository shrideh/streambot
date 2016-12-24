'''
boss.py

boss is zeromq based, multi-threading Task distributing framework

Interfaces:
start()
stop()

Classes:
Task

Enum:
TaskStatus
'''
import time
import zmq
import uuid
import threading
import logging


logger = logging.getLogger(__name__)
_CONTEXT = zmq.Context()

# TODO: handle failed tasks


_GLOBAL_PRINT_LOCK = threading.Lock()
_GLOBAL_TASK_LOCK = threading.Lock()
_WORKER_TASK = 'WORKER_TASK'
_WORKER_RESULT = 'WORKER_RESULT'
_WORKER_ACK = 'WORKER_ACK'

_WORKING_THREADS = []   # list of working threads

_TASK_OUT_SOCKET = None  # PUSH socket for dispatching _TASKS to workers
_TASKS = {}  # All tasks, key: Task.id, value: Task object


def _print(message, block=True):
    if block:
        _GLOBAL_PRINT_LOCK.acquire()
    print(message)
    if block:
        _GLOBAL_PRINT_LOCK.release()


class Task(object):
    def __init__(self, task_id, command, status='START'):
        '''
        @param task_id Unique id for a task
        @param command An object

        'STOP' status reserved for stopping working thread
        '''
        self.id = task_id
        self.command = command
        self.status = status

    def set_done(self):
        self.status = 'DONE'

    def is_done(self):
        return 'DONE' == self.status

    def set_failed(self):
        self.status = 'FAILED'

    def is_failed(self):
        return 'FAILED' == self.status

    def __repr__(self):
        return 'task {id}: stauts {status}'.format(id=self.id, status=self.status)


class _WorkerThread(threading.Thread):
    '''
    boss's worker thread

    read Task from task_in socket, which binds to inproc://WORKER_TASK
    send Task result to result_out socket, which binds to inproc://WORKER_RESULT
    also sync to client via worker_ack_out socket, wich binds to inproc://WORKER_ACK
    also command socket, which binds to inproc://{id}

    action is the Task handler: bool(Task)
    '''
    def __init__(self, action):
        '''
        @param action A function implements "bool (Task)"
        '''
        threading.Thread.__init__(self)
        self.id = uuid.uuid4()
        self.task_in = _CONTEXT.socket(zmq.PULL)
        self.result_out = _CONTEXT.socket(zmq.REQ)
        self.worker_ack_out = _CONTEXT.socket(zmq.REQ)
        self.command_in = _CONTEXT.socket(zmq.PULL)
        self.command_out = _CONTEXT.socket(zmq.PUSH)
        self.command_in.connect('inproc://{id}'.format(id=self.id))
        self.command_out.bind('inproc://{id}'.format(id=self.id))
        self.poller = zmq.Poller()
        self.poller.register(self.command_in, zmq.POLLIN)
        self.poller.register(self.task_in, zmq.POLLIN)

        self.action = action
        _print('create worker [{id}]'.format(id=self.id))

    def run(self):
        try:
            # init sockets
            self.task_in.connect('inproc://{proc_name}'.format(proc_name=_WORKER_TASK))
            self.result_out.connect('inproc://{proc_name}'.format(proc_name=_WORKER_RESULT))

            # sync worker to boss
            self.worker_ack_out.connect('inproc://{proc_name}'.format(proc_name=_WORKER_ACK))
            self.worker_ack_out.send(b'')
            _print('waiting to start worker [{id}]'.format(id=self.id))
            self.worker_ack_out.recv()  # blocking wait client to response, then start working process
            _print('worker [{id}] stats'.format(id=self.id))

            # main working loop
            while True:
                _print('worker [{id}] is waiting for task'.format(id=self.id))
                socks = dict(self.poller.poll())
                if self.command_in in socks and socks[self.command_in] == zmq.POLLIN:
                    _print('stop() received')
                    break

                if self.task_in in socks and socks[self.task_in] == zmq.POLLIN:
                    task_msg = self.task_in.recv_json()
                    _print('receive task_msg: {msg}'.format(msg=task_msg))
                    if 'STOP' == task_msg['status']:
                        break

                    task = Task(task_msg['id'], task_msg['command'])
                    _print('worker [{id}] is working on {task}'.format(id=self.id, task=task.id))

                    if self.action(task):
                        task.set_done()

                    self.result_out.send_json(task.__dict__)

                    _print('worker [{id}] is sending out result'.format(id=self.id))
                    self.result_out.recv()
        except Exception as e:
            _print('worker [{id}] Error: {error}'.format(id=self.id, error=e.strerror))

    def stop(self):
        '''
        properly stopping a _WorkerThread is:

        worker.stop()
        worker.join()
        '''
        self.command_out.send('STOP')


class _SinkerThread(threading.Thread):
    '''
    Sinker thread

    receive Task result from result_in socket, which binds to inproc://WORKER_RESULT
    update global _TASKS dict
    '''
    def __init__(self):
        threading.Thread.__init__(self)
        self.id = uuid.uuid4()
        self.result_in = _CONTEXT.socket(zmq.REP)
        self.command_in = _CONTEXT.socket(zmq.PULL)
        self.command_out = _CONTEXT.socket(zmq.PUSH)
        self.command_in.connect('inproc://{id}'.format(id=self.id))
        self.command_out.bind('inproc://{id}'.format(id=self.id))
        self.poller = zmq.Poller()
        self.poller.register(self.command_in, zmq.POLLIN)
        self.poller.register(self.result_in, zmq.POLLIN)
        _print('create sinker [{id}]'.format(id=self.id))

    def run(self):
        try:
            global _TASKS
            self.result_in.bind('inproc://{proc_name}'.format(proc_name=_WORKER_RESULT))

            while True:
                socks = dict(self.poller.poll())
                if self.command_in in socks and socks[self.command_in] == zmq.POLLIN:
                    _print('stop() received')
                    break

                if self.result_in in socks and socks[self.result_in] == zmq.POLLIN:
                    task_msg = self.result_in.recv_json()

                    task = Task(task_msg['id'], task_msg['command'], task_msg['status'])
                    _print('sink [{id}] received result of task {task_id}'.format(id=self.id, task_id=task.id))
                    _GLOBAL_TASK_LOCK.acquire()
                    _TASKS[task.id] = task
                    _GLOBAL_TASK_LOCK.release()
                    self.result_in.send(b'')

        except Exception as e:
            _print('sink [{id}] Error: {error}'.format(id=self.id, error=e.strerror))

    def stop(self):
        self.command_out.send('STOP')


def _sync_workers(ack_in, num_workers):
    '''
    synchronise active workers
    @param ack_in worker thread ack in socket, binds to inproc://_WORKER_ACK
    @param num_workers Number of workers
    '''
    num_active_workers = 0
    while num_active_workers < num_workers:
        _print('sync with worker {n}'.format(n=num_active_workers))
        ack_in.recv()
        ack_in.send(b'')
        num_active_workers += 1


def start(action, num_workers=3):
    '''
    @param action bool(Task)
    @num_workers Number workers, default 3
    '''
    global _WORKING_THREADS

    # bind worker ack
    ack_in = _CONTEXT.socket(zmq.REP)
    ack_in.bind('inproc://{proc_name}'.format(proc_name=_WORKER_ACK))

    # create sink
    sinker_thread = _SinkerThread()
    _WORKING_THREADS.append(sinker_thread)
    sinker_thread.start()

    # create workers
    _print('create workers')
    for i in range(num_workers):
        worker_thread = _WorkerThread(action=action)
        _WORKING_THREADS.append(worker_thread)
        worker_thread.start()

    try:
        _print('sync workers')
        _sync_workers(ack_in, num_workers)

        # create _TASK_OUT_SOCKET
        global _TASK_OUT_SOCKET
        _TASK_OUT_SOCKET = _CONTEXT.socket(zmq.PUSH)
        _TASK_OUT_SOCKET.bind('inproc://{proc_name}'.format(proc_name=_WORKER_TASK))
    except Exception as e:
        _print('Error in start worker {error}'.format(error=e))
        stop()


def stop():
    '''
    stop all working threads, including workers and sinker
    '''
    for t in _WORKING_THREADS:
        _print('Stopping thread {id}'.format(id=t.id))
        t.stop()
        t.join()


def assign_task(task):
    '''
    Assign task to the active worker
    @param task Task object
    '''
    global _TASK_OUT_SOCKET
    global _TASKS
    if not _TASK_OUT_SOCKET:
        _print('Error _TASK_OUT_SOCKET is None. start() the boss')
        return

    _GLOBAL_TASK_LOCK.acquire()
    if task.id in _TASKS:
        _print('task: {task_id} is processed'.format(task_id=task.id))
        _GLOBAL_TASK_LOCK.release()
    else:
        _TASKS[task.id] = task
        _GLOBAL_TASK_LOCK.release()
        _print('send task: {task}'.format(task=task.id))
        _TASK_OUT_SOCKET.send_json(task.__dict__)


def have_all_tasks_done():
    '''
    Check whether all tasks done
    '''
    _GLOBAL_TASK_LOCK.acquire()
    all_TASKS_done = True
    for k, v, in _TASKS.items():
        if not v.is_done():
            all_TASKS_done = False
            break
    _GLOBAL_TASK_LOCK.release()
    return all_TASKS_done


def tasks():
    return _TASKS


def main():
    def simple_action(task):
        '''
        @param task Task instance
        @return True indicating task done, False otherwise
        '''
        _print('procesing task: {id}'.format(id=task.id))
        import random
        time.sleep(random.randint(1, 10))
        return True

    # start boss (including worker and sinker processes)
    start(num_workers=2, action=simple_action)

    # dispatch dummy tasks
    for i in range(5):
        import random
        task_id = random.randint(1, 30)
        task = Task(task_id, {})
        assign_task(task)
        time.sleep(0.5)

    # check all tasks done before stop boss
    total_check = 0
    while not have_all_tasks_done():
        _print('Waiting for all _TASKS done')
        time.sleep(1)
        total_check += 1
        if total_check > 50:
            break

    # stop boss (including worker and sinker processes)
    stop()

    all_tasks = tasks()
    print(all_tasks)


if __name__ == '__main__':
    main()
