#!/usr/bin/env python3
"""
Visor de espectro para el enlace ESP32-S3 <-> Raspberry Pi.

"""

import argparse
import math
import re
import shlex
import socket
import sys
import threading
import time

import paramiko
import pyqtgraph as pg
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_kv(text: str) -> dict:
    """'a=1 b=2.5' -> {'a': '1', 'b': '2.5'}"""
    return dict(KV_RE.findall(text))


def parse_line(line: str):
    """Interpreta una linea del char device de datos.

    Devuelve ("SWEEP", {clave: float}) | ("POINT", (freq, db)) | None.
    """
    line = line.strip()
    try:
        if line.startswith("SWEEP"):
            return "SWEEP", {k: float(v) for k, v in parse_kv(line).items()}
        if line.startswith("POINT"):
            kv = parse_kv(line)
            return "POINT", (float(kv["freq"]), float(kv["db"]))
    except (KeyError, ValueError):
        pass
    return None


class SSHLink(QThread):
    """Mantiene la conexion SSH, lee el device de datos y envia comandos."""

    sweep = Signal(dict)            # cabecera de barrido
    sample = Signal(float, float)   # frecuencia, potencia (dB)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, args, parent=None):
        super().__init__(parent)
        self.args = args
        self.sudo = "sudo -n " if args.sudo else ""
        self.client: paramiko.SSHClient | None = None
        self._running = True

    # ---------- lectura continua ----------
    def run(self):
        a = self.args
        try:
            self.status.emit(f"Conectando a {a.host}...")
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self.client.connect(
                a.host, port=a.port, username=a.user,
                password=a.password, key_filename=a.key, timeout=10,
            )
            transport = self.client.get_transport()
            transport.set_keepalive(15)
            self.status.emit(f"Conectado. Leyendo {a.data_device}")

            # Si el cat termina (EOF) sin error, se vuelve a lanzar.
            while self._running:
                chan = transport.open_session()
                chan.exec_command(f"{self.sudo}cat {shlex.quote(a.data_device)}")
                chan.settimeout(1.0)
                buf = b""
                while self._running:
                    try:
                        data = chan.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    buf += data
                    *lines, buf = buf.split(b"\n")
                    for raw in lines:
                        self._handle(raw)
                if buf.strip():
                    self._handle(buf)

                err = ""
                if chan.recv_stderr_ready():
                    err = chan.recv_stderr(4096).decode(errors="ignore").strip()
                chan.close()
                if err:
                    self.failed.emit(self._explain(err))
                    return
                if self._running:
                    time.sleep(0.3)
        except Exception as e:
            self.failed.emit(f"Error SSH: {e}")
        finally:
            if self.client:
                self.client.close()

    def _handle(self, raw: bytes):
        parsed = parse_line(raw.decode(errors="ignore"))
        if not parsed:
            return
        kind, payload = parsed
        if kind == "SWEEP":
            self.sweep.emit(payload)
        else:
            self.sample.emit(*payload)

    @staticmethod
    def _explain(err: str) -> str:
        if "Permission denied" in err:
            return ("Permiso denegado sobre el device. Dar permisos (regla udev / grupo) "
                    "o usar --sudo con sudo sin password.")
        if "password is required" in err:
            return "sudo pide password: configurar NOPASSWD o usar una regla udev."
        return f"Error en la Raspberry: {err}"

    # ---------- comando + respuesta ----------
    def _exec(self, cmd: str, timeout=10):
        _, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode(errors="ignore")
        err = stderr.read().decode(errors="ignore").strip()
        return stdout.channel.recv_exit_status(), out, err

    def send_command(self, command: str, on_done):
        """Escribe 'command' en el device de comandos y lee la respuesta.
        Corre en un hilo aparte. on_done(ok, mensaje, respuesta)."""

        def worker():
            q = shlex.quote
            try:
                if not self.client:
                    raise RuntimeError("Sin conexion SSH")
                write = (f"printf '%s\\n' {q(command)} | "
                         f"{self.sudo}tee {q(self.args.cmd_device)} > /dev/null")
                rc, _, err = self._exec(write)
                if rc != 0:
                    on_done(False, self._explain(err) if err else f"Fallo al escribir (rc={rc})", "")
                    return
                read = f"{self.sudo}timeout 3 cat {q(self.args.cmd_device)}"
                _, out, err = self._exec(read, timeout=8)
                if not out.strip() and err:
                    on_done(False, self._explain(err), "")
                    return
                on_done(True, f"Comando enviado: {command}", out.strip())
            except Exception as e:
                on_done(False, f"Error: {e}", "")

        threading.Thread(target=worker, daemon=True).start()

    def stop(self):
        self._running = False


class MainWindow(QMainWindow):
    cmd_result = Signal(bool, str, str)

    def __init__(self, args):
        super().__init__()
        self.setWindowTitle("Espectro - ESP32 link")
        self.resize(1050, 560)

        self.args = args
        self.div = args.freq_div
        self.points: dict[float, float] = {}   # frecuencia -> dB
        self.f_range = None                     # (fmin, fmax) en unidades crudas
        self.expected = 0
        self.dirty = False

        # --- Grafico ---
        pg.setConfigOptions(antialias=True)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", f"Frecuencia ({args.freq_unit})")
        self.plot.setLabel("left", "Potencia (dB)")
        self.plot.setLogMode(x=True, y=False)
        self.curve = self.plot.plot(
            pen=None, symbol="o", symbolSize=8,
            symbolBrush=pg.mkBrush(30, 100, 220, 220), symbolPen=None,
        )

        # --- Panel lateral ---
        self.btn_cfg = QPushButton("Get config")
        self.btn_cfg.setMinimumHeight(40)
        self.btn_cfg.clicked.connect(self.on_send)

        self.chk_log = QCheckBox("Eje X logaritmico")
        self.chk_log.setChecked(True)
        self.chk_log.toggled.connect(self.on_log_toggled)

        self.btn_clear = QPushButton("Limpiar grafico")
        self.btn_clear.clicked.connect(self.clear_points)

        self.lbl_cfg = QLabel("Config: -")
        self.lbl_count = QLabel("Puntos: 0")
        self.lbl_status = QLabel("Iniciando...")
        for lbl in (self.lbl_cfg, self.lbl_status):
            lbl.setWordWrap(True)

        side = QVBoxLayout()
        side.addWidget(self.btn_cfg)
        side.addWidget(self.chk_log)
        side.addWidget(self.btn_clear)
        side.addSpacing(15)
        side.addWidget(self.lbl_cfg)
        side.addWidget(self.lbl_count)
        side.addSpacing(15)
        side.addWidget(self.lbl_status)
        side.addStretch()
        side_widget = QWidget()
        side_widget.setLayout(side)
        side_widget.setFixedWidth(240)

        layout = QHBoxLayout()
        layout.addWidget(self.plot, stretch=1)
        layout.addWidget(side_widget)
        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        # --- Enlace SSH ---
        self.cmd_result.connect(self.on_cmd_result)
        self.link = SSHLink(args)
        self.link.sweep.connect(self.on_sweep)
        self.link.sample.connect(self.on_sample)
        self.link.status.connect(self.lbl_status.setText)
        self.link.failed.connect(self.lbl_status.setText)
        self.link.start()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_plot)
        self.timer.start(50)

    # ---------- datos ----------
    def on_sweep(self, info: dict):
        """Llega la cabecera de un barrido nuevo: se reinicia el grafico."""
        self.points.clear()
        self.dirty = True
        self.apply_config(info)

    def on_sample(self, freq: float, db: float):
        self.points[freq / self.div] = db
        self.dirty = True

    def refresh_plot(self):
        if not self.dirty:
            return
        self.dirty = False
        xs = sorted(self.points)
        self.curve.setData(xs, [self.points[x] for x in xs])
        total = f"/{self.expected}" if self.expected else ""
        self.lbl_count.setText(f"Puntos: {len(xs)}{total}")

    def clear_points(self):
        self.points.clear()
        self.dirty = True

    # ---------- configuracion ----------
    def apply_config(self, cfg: dict):
        """cfg con frec_inicio, frec_final, puntos, tiempo (valores numericos)."""
        try:
            fmin, fmax = float(cfg["frec_inicio"]), float(cfg["frec_final"])
        except (KeyError, ValueError):
            return
        self.f_range = (fmin, fmax)
        self.expected = int(float(cfg.get("puntos", 0)))
        unit = self.args.freq_unit
        self.lbl_cfg.setText(
            f"Config: {fmin / self.div:g} - {fmax / self.div:g} {unit}, "
            f"{self.expected} puntos, tiempo={cfg.get('tiempo', '?')}"
        )
        self.apply_x_range()

    def apply_x_range(self):
        if not self.f_range:
            self.plot.enableAutoRange(axis="x")
            return
        lo, hi = (v / self.div for v in self.f_range)
        if self.chk_log.isChecked():
            if lo <= 0:
                self.plot.enableAutoRange(axis="x")
                return
            lo, hi = math.log10(lo), math.log10(hi)
        self.plot.setXRange(lo, hi, padding=0.05)

    def on_log_toggled(self, checked: bool):
        self.plot.setLogMode(x=checked, y=False)
        self.apply_x_range()

    # ---------- boton ----------
    def on_send(self):
        self.btn_cfg.setEnabled(False)
        self.lbl_status.setText("Enviando comando...")
        self.link.send_command(
            self.args.command,
            lambda ok, msg, resp: self.cmd_result.emit(ok, msg, resp),
        )

    def on_cmd_result(self, ok: bool, msg: str, response: str):
        self.btn_cfg.setEnabled(True)
        self.lbl_status.setText(msg)
        if ok and response:
            kv = parse_kv(response)
            try:
                self.apply_config({k: float(v) for k, v in kv.items()})
            except ValueError:
                self.lbl_status.setText(f"{msg}\nRespuesta: {response}")

    def closeEvent(self, event):
        self.timer.stop()
        self.link.stop()
        self.link.wait(3000)
        super().closeEvent(event)


def main():
    p = argparse.ArgumentParser(description="Visor de espectro ESP32 link via SSH")
    p.add_argument("--host", default="lse-pi4-04.local", help="IP/hostname de la Raspberry")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--user", default="lse")
    p.add_argument("--password", default="lse")
    p.add_argument("--key", default=None, help="Ruta a la clave privada SSH")
    p.add_argument("--data-device", default="/dev/esp32link_data")
    p.add_argument("--cmd-device", default="/dev/esp32link_cmd")
    p.add_argument("--command", default="get config")
    p.add_argument("--sudo", action="store_true",
                   help="Usar 'sudo -n' para acceder a los devices (requiere NOPASSWD)")
    p.add_argument("--freq-div", type=float, default=1.0,
                   help="Divisor de frecuencia para mostrar (1 = Hz; 1000 = kHz)")
    p.add_argument("--freq-unit", default="Hz")
    args = p.parse_args()

    app = QApplication(sys.argv)
    win = MainWindow(args)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
