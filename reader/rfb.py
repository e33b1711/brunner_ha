"""Minimaler RFB-(VNC-)Client: holt genau ein vollstaendiges Bild und kann klicken.

Die BHZ verlangt kein Passwort (Security-Typ "None"). Jeder Aufruf baut eine
eigene Verbindung auf, weil die BHZ inkrementelle Updates nur traege liefert.
"""
import socket
import struct

from PIL import Image


class RFBError(Exception):
    pass


class RFB:
    def __init__(self, host, port=5900, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self._handshake()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RFBError("Verbindung von der BHZ geschlossen")
            buf += chunk
        return bytes(buf)

    def _handshake(self):
        banner = self._recv(12)
        if not banner.startswith(b"RFB "):
            raise RFBError(f"kein VNC-Server: {banner!r}")
        self.sock.sendall(b"RFB 003.008\n")
        ntypes = self._recv(1)[0]
        if ntypes == 0:
            (length,) = struct.unpack(">I", self._recv(4))
            raise RFBError(self._recv(length).decode(errors="replace"))
        types = self._recv(ntypes)
        if 1 not in types:
            raise RFBError(f"BHZ verlangt Authentifizierung (Typen {list(types)})")
        self.sock.sendall(b"\x01")
        (result,) = struct.unpack(">I", self._recv(4))
        if result != 0:
            raise RFBError("Anmeldung abgelehnt")
        self.sock.sendall(b"\x01")  # ClientInit: shared
        self.width, self.height = struct.unpack(">HH", self._recv(4))
        self._recv(16)  # Pixelformat des Servers, wird gleich ueberschrieben
        (namelen,) = struct.unpack(">I", self._recv(4))
        self.name = self._recv(namelen).decode(errors="replace")
        # 32 Bit, little endian, Bytefolge im Speicher: B, G, R, X
        self.sock.sendall(struct.pack(">BxxxBBBBHHHBBBxxx", 0, 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
        self.sock.sendall(struct.pack(">BxHi", 2, 1, 0))  # SetEncodings: nur Raw

    def capture(self):
        """Fordert ein vollstaendiges (nicht inkrementelles) Bild an."""
        w, h = self.width, self.height
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))
        img = Image.new("RGB", (w, h))
        covered = 0
        while covered < w * h:
            msg = self._recv(1)[0]
            if msg == 0:  # FramebufferUpdate
                (nrects,) = struct.unpack(">xH", self._recv(3))
                for _ in range(nrects):
                    x, y, rw, rh, enc = struct.unpack(">HHHHi", self._recv(12))
                    if enc != 0:
                        raise RFBError(f"unerwartetes Encoding {enc}")
                    data = self._recv(rw * rh * 4)
                    img.paste(Image.frombuffer("RGB", (rw, rh), data, "raw", "BGRX", 0, 1), (x, y))
                    covered += rw * rh
            elif msg == 1:  # SetColourMapEntries
                _, ncol = struct.unpack(">xHH", self._recv(5))
                self._recv(ncol * 6)
            elif msg == 2:  # Bell
                pass
            elif msg == 3:  # ServerCutText
                (length,) = struct.unpack(">xxxI", self._recv(7))
                self._recv(length)
            else:
                raise RFBError(f"unbekannte Servernachricht {msg}")
        return img

    def click(self, x, y):
        import time
        for mask in (0, 1, 0):
            self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))
            time.sleep(0.15)


def capture(host, port=5900):
    with RFB(host, port) as c:
        return c.capture()
