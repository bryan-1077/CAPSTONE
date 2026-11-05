"""Private local request/response channel; responses exist only in memory."""
import json
import os
import socket
import threading
import uuid

MAX_MESSAGE = 1024 * 1024


def receive(stream):
    raw = stream.readline(MAX_MESSAGE + 1)
    if not raw.endswith(b'\n') or len(raw) > MAX_MESSAGE:
        raise ValueError('Invalid or oversized interaction message')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('Unsupported interaction protocol')
    return value


def rpc(path, command, **payload):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(0.5)
        connection.connect(str(path))
        connection.sendall(json.dumps(dict(version=1, command=command, **payload)).encode() + b'\n')
        with connection.makefile('rb') as stream:
            return receive(stream)


class Interaction:
    def __init__(self, path, worker_id=None, cancel_callback=None):
        self.path = str(path)
        self.worker_id = worker_id
        self.cancel_callback = cancel_callback
        self.cancelled = False
        self.condition = threading.Condition()
        self.pending = None
        self.response = None
        self.closed = False
        self.server = socket.socket(socket.AF_UNIX)
        try:
            self.server.bind(self.path)
        except OSError:
            self.server.close()
            raise
        os.chmod(self.path, 0o600)
        self.server.listen(2)
        self.server.settimeout(0.2)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed:
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.5)
                try:
                    with connection.makefile('rb') as stream:
                        command = receive(stream)
                    with self.condition:
                        if command.get('command') == 'poll':
                            result = dict(pending=self.pending, worker_id=self.worker_id,
                                          lifecycle='cancelling' if self.cancelled else 'running')
                        elif command.get('command') == 'cancel':
                            if command.get('worker_id') != self.worker_id or self.cancel_callback is None:
                                result = dict(error='Worker identity mismatch or cancellation unavailable')
                            else:
                                first = not self.cancelled
                                self.cancelled = True
                                self.pending = None
                                self.response = None
                                self.condition.notify_all()
                                result = dict(accepted=True)
                                if first:
                                    self.cancel_callback()
                        elif command.get('command') == 'respond':
                            if not self.pending or command.get('request_id') != self.pending['request_id'] or self.response is not None:
                                result = dict(error='Stale or duplicate response')
                            else:
                                self.response = command.get('response', {})
                                self.condition.notify_all()
                                result = dict(accepted=True)
                        else:
                            result = dict(error='Unknown command')
                    connection.sendall(json.dumps(dict(version=1, **result)).encode() + b'\n')
                except (OSError, ValueError):
                    pass

    def request(self, kind, context):
        with self.condition:
            if self.closed or self.cancelled:
                raise ValueError('Interaction channel closed or cancelled')
            self.pending = dict(request_id=uuid.uuid4().hex, kind=kind, context=context)
            self.response = None
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.response is not None or self.closed or self.cancelled)
            response = self.response
            self.pending = None
            self.response = None
            if self.closed or self.cancelled or not isinstance(response, dict) or response.get('action') == 'cancel':
                raise ValueError('User cancelled input; flow stopped')
            return response

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.server.close()
        self.thread.join(timeout=1)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
