"""Short, in-memory messages for browsers paired with a Listening Room host."""

import threading
import time


class AnnouncementBoard:
    def __init__(self, now=time.time):
        self.now = now
        self.lock = threading.Lock()
        self.messages = []
        self.next_id = 1

    def post(self, message, recipient=""):
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 280:
            raise ValueError("Write an announcement of 1 to 280 characters")
        message = " ".join(message.split())
        if any(ord(character) < 32 for character in message):
            raise ValueError("Announcement contains unsupported characters")
        with self.lock:
            item = {"id": self.next_id, "message": message, "recipient": recipient,
                    "sent_at": self.now(), "sender": "Listening Room host"}
            self.next_id += 1
            self.messages.append(item)
            self.messages = self.messages[-50:]
            return dict(item)

    def for_device(self, device_id):
        with self.lock:
            cutoff = self.now() - 24 * 60 * 60
            return [dict(item) for item in self.messages
                    if item["sent_at"] >= cutoff and (not item["recipient"] or item["recipient"] == device_id)]
