"""
The Salt Factories Log Server is responsible to receive log records from the salt daemons.

Because all of Salt's daemons and CLI tools are started in subprocesses, there's really no easy way to get those logs
into the main process where the test suite is running.

However, salt is extensible by nature, and it provides a way to attach custom log handlers into python's logging
machinery.

We take advantage of that and add a custom logging handler into subprocesses we start for salt. That logging handler
will then forward **all** log records into this log server, which in turn, injects them into the logging machinery
running in the test suite.

This allows one to use PyTest's :fixture:`caplog fixture <pytest:caplog>` to assert against log messages.

As an example:

.. code-block:: python

    def test_baz(caplog):
        func_under_test()
        for record in caplog.records:
            assert record.levelname != "CRITICAL"
        assert "wally" not in caplog.text
"""
import logging
import threading

import attr
import msgpack
import pytest
import zmq
from pytestshellutils.utils import ports
from pytestshellutils.utils import time
from pytestskipmarkers.utils import platform

log = logging.getLogger(__name__)


@attr.s(kw_only=True, slots=True, hash=True)
class LogServer:
    """
    Log server plugin.
    """

    log_host = attr.ib()
    log_port = attr.ib()
    log_level = attr.ib()
    socket_hwm = attr.ib()
    running_event = attr.ib(init=False, repr=False, hash=False)
    sentinel_event = attr.ib(init=False, repr=False, hash=False)
    process_queue_thread = attr.ib(init=False, repr=False, hash=False)

    @log_host.default
    def _default_log_host(self):
        if platform.is_windows():
            # Windows cannot bind to 0.0.0.0
            return "127.0.0.1"
        return "0.0.0.0"

    @log_port.default
    def _default_log_port(self):
        return ports.get_unused_localhost_port()

    @socket_hwm.default
    def _default_socket_hwm(self):
        # ~1MB
        return 1000000

    def start(self):
        """
        Start the log server.
        """
        log.info("%s starting...", self)
        self.sentinel_event = threading.Event()
        self.running_event = threading.Event()
        self.process_queue_thread = threading.Thread(target=self.process_logs)
        self.process_queue_thread.start()
        # Wait for the thread to start
        if self.running_event.wait(5) is not True:  # pragma: no cover
            self.running_event.clear()
            raise RuntimeError("Failed to start the log server")
        log.info("%s started", self)

    def stop(self):
        """
        Stop the log server.
        """
        log.info("%s stopping...", self)
        address = "tcp://{}:{}".format(self.log_host, self.log_port)
        context = zmq.Context()
        sender = context.socket(zmq.PUSH)  # pylint: disable=no-member
        sender.connect(address)
        try:
            sender.send(msgpack.dumps(None))
            log.debug("%s Sent sentinel to trigger log server shutdown", self)
            if self.sentinel_event.wait(5) is not True:  # pragma: no cover
                log.warning(
                    "%s Failed to wait for the reception of the stop sentinel message. Stopping anyway.",
                    self,
                )
        finally:
            sender.close(1000)
            context.term()

        # Clear the running even, the log process thread know it should stop
        self.running_event.clear()
        log.info("%s Joining the logging server process thread", self)
        self.process_queue_thread.join(7)
        if not self.process_queue_thread.is_alive():
            log.debug("%s Stopped", self)
        else:  # pragma: no cover
            log.warning(
                "%s The logging server thread is still running. Waiting a little longer...", self
            )
            self.process_queue_thread.join(5)
            if not self.process_queue_thread.is_alive():
                log.debug("%s Stopped", self)
            else:
                log.warning("%s The logging server thread is still running...", self)

    def process_logs(self):
        """
        Process the logs returned.
        """
        address = "tcp://{}:{}".format(self.log_host, self.log_port)
        context = zmq.Context()
        puller = context.socket(zmq.PULL)  # pylint: disable=no-member
        puller.set_hwm(self.socket_hwm)
        exit_timeout_seconds = 5
        exit_timeout = None
        if msgpack.version >= (0, 5, 2):
            msgpack_kwargs = dict(raw=False)
        else:  # pragma: no cover
            msgpack_kwargs = dict(encoding="utf-8")
        try:
            puller.bind(address)
        except zmq.ZMQError:  # pragma: no cover
            log.exception("%s Unable to bind to puller at %s", self, address)
            return
        try:
            self.running_event.set()
            poller = zmq.Poller()
            poller.register(puller, zmq.POLLIN)
            while True:
                if not self.running_event.is_set():
                    if exit_timeout is None:
                        log.debug(
                            "%s Waiting %d seconds to process any remaning log messages "
                            "before exiting...",
                            self,
                            exit_timeout_seconds,
                        )
                        exit_timeout = time.time() + exit_timeout_seconds

                    if time.time() >= exit_timeout:
                        log.debug(
                            "%s Unable to process remaining log messages in time. Exiting anyway.",
                            self,
                        )
                        break
                try:
                    if not poller.poll(1000):
                        continue
                    msg = puller.recv()
                    record_dict = msgpack.loads(msg, **msgpack_kwargs)
                    if record_dict is None:
                        # A sentinel to stop processing the queue
                        log.info("%s Received the sentinel to shutdown", self)
                        self.sentinel_event.set()
                        break
                    try:
                        record_dict["message"]
                    except KeyError:  # pragma: no cover
                        # This log record was msgpack dumped from Py2
                        for key, value in record_dict.copy().items():
                            skip_update = True
                            if isinstance(value, bytes):
                                value = value.decode("utf-8")
                                skip_update = False
                            if isinstance(key, bytes):
                                key = key.decode("utf-8")
                                skip_update = False
                            if skip_update is False:
                                record_dict[key] = value
                    # Just log everything, filtering will happen on the main process
                    # logging handlers
                    record = logging.makeLogRecord(record_dict)
                    logger = logging.getLogger(record.name)
                    logger.handle(record)
                except (EOFError, KeyboardInterrupt, SystemExit):  # pragma: no cover
                    break
                except Exception as exc:  # pragma: no cover pylint: disable=broad-except
                    log.warning(
                        "%s An exception occurred in the processing queue thread: %s",
                        self,
                        exc,
                        exc_info=True,
                    )
        finally:
            puller.close(1)
            context.term()
        log.debug("%s Process log thread terminated", self)


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    """
    Configure the pytest plugin.
    """
    # If PyTest has no logging configured, default to ERROR level
    levels = [logging.ERROR]
    logging_plugin = config.pluginmanager.get_plugin("logging-plugin")
    try:
        level = logging_plugin.log_cli_handler.level
        if level is not None:
            levels.append(level)
    except AttributeError:  # pragma: no cover
        # PyTest CLI logging not configured
        pass
    try:
        level = logging_plugin.log_file_level
        if level is not None:
            levels.append(level)
    except AttributeError:  # pragma: no cover
        # PyTest Log File logging not configured
        pass

    if logging.NOTSET in levels:
        # We don't want the NOTSET level on the levels
        levels.pop(levels.index(logging.NOTSET))

    log_level = logging.getLevelName(min(levels))

    log_server = LogServer(log_level=log_level)
    config.pluginmanager.register(log_server, "saltfactories-log-server")


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    """
    Start the pytest plugin.
    """
    log_server = session.config.pluginmanager.get_plugin("saltfactories-log-server")
    log_server.start()


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session):
    """
    Stop the pytest plugin.
    """
    log_server = session.config.pluginmanager.get_plugin("saltfactories-log-server")
    log_server.stop()
