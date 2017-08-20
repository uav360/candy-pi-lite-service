# -*- coding: utf-8 -*-

# Copyright (c) 2017 CANDY LINE INC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import fcntl
import json
import os
import signal
import socket
import select
import struct
import sys
import termios
import threading
import time
import subprocess
import atexit
import re
import candy_board_qws
import logging
import logging.handlers
from croniter import croniter

# sys.argv[0] ... Serial Port
# sys.argv[1] ... The path to socket file,
#                 e.g. /var/run/candy-board-service.sock
# sys.argv[2] ... The network interface name to be monitored

LED = 'gpio%s' % (os.environ['LED2'] if 'LED2' in os.environ else '4')
PIDFILE = '/var/run/candy-pi-lite-service.pid'
logger = logging.getLogger('candy-pi-lite')
logger.setLevel(logging.INFO)
handler = logging.handlers.SysLogHandler(address='/dev/log')
logger.addHandler(handler)
formatter = logging.Formatter('%(module)s.%(funcName)s: %(message)s')
handler.setFormatter(formatter)
led = 0
led_sec = float(os.environ['BLINKY_INTERVAL_SEC']) \
    if 'BLINKY_INTERVAL_SEC' in os.environ else 1.0
if led_sec < 0 or led_sec > 60:
    led_sec = 1.0
PPP_PING_INTERVAL_SEC = float(os.environ['PPP_PING_INTERVAL_SEC']) \
    if 'PPP_PING_INTERVAL_SEC' in os.environ else 0.0
online = False
shutdown_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '__shutdown')
PID = str(os.getpid())


class Pinger(threading.Thread):
    DEST_ADDR = '<broadcast>'
    DEST_PORT = 60100
    CAT_PPP0_TX_STAT = 'cat /sys/class/net/ppp0/statistics/tx_bytes'

    def __init__(self, interval_sec):
        super(Pinger, self).__init__()
        self.interval_sec = interval_sec
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind(('', 0))
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.last_tx_bytes = 0

    def run(self):
        while self.interval_sec >= 5:
            if not os.path.isfile(Pinger.CAT_PPP0_TX_STAT):
                time.sleep(self.interval_sec)
                continue
            try:
                self.tx_bytes = subprocess.Popen(Pinger.CAT_PPP0_TX_STAT,
                                                 shell=True,
                                                 stdout=subprocess.PIPE
                                                 ).stdout.read()
                if int(self.tx_bytes) != self.last_tx_bytes:
                    self.socket.sendto('',
                                       (Pinger.DEST_ADDR, Pinger.DEST_PORT))
                    time.sleep(self.interval_sec)
                    self.last_tx_bytes = int(self.tx_bytes)
            except Exception:
                time.sleep(self.interval_sec)
                pass


class Monitor(threading.Thread):
    FNULL = open(os.devnull, 'w')

    def __init__(self, nic):
        super(Monitor, self).__init__()
        self.nic = nic
        try:
            self.restart_at = None
            cron = croniter(os.environ['RESTART_SCHEDULE_CRON']) \
                if 'RESTART_SCHEDULE_CRON' in os.environ else None
            if cron:
                self.restart_at = cron.get_next()
                if self.restart_at - time.time() < 60:
                    self.restart_at = cron.get_next()
                logger.info(
                    "candy-pi-lite service will restart within %d seconds" %
                    (self.restart_at - time.time()))
        except Exception:
            logger.warn("RESTART_SCHEDULE_CRON=>[%s] is ignored"
                        % os.environ['RESTART_SCHEDULE_CRON'])

    def terminate(self, restart=False):
        if os.path.isfile(shutdown_state_file):
            return False
        # exit from non-main thread
        if restart:
            logger.error("candy-pi-lite service will be restarted...")
            os.kill(os.getpid(), signal.SIGQUIT)
        else:
            logger.error("candy-pi-lite service is terminated. Shutting down.")
            os.kill(os.getpid(), signal.SIGTERM)
        return True

    def time_to_restart(self):
        if self.restart_at is None:
            return False
        return self.restart_at <= time.time()

    def run(self):
        global online
        while True:
            try:
                if self.time_to_restart():
                    if self.terminate(True):
                        return
                if not os.path.isfile(PIDFILE):
                    if self.terminate():
                        return
                err = subprocess.call("ip link show %s" % self.nic,
                                      shell=True,
                                      stdout=Monitor.FNULL,
                                      stderr=subprocess.STDOUT)
                online = (err == 0)
                if not online:
                    time.sleep(5)
                    continue

                err = subprocess.call("ip route | grep default | grep -v %s" %
                                      self.nic, shell=True,
                                      stdout=Monitor.FNULL,
                                      stderr=subprocess.STDOUT)
                if err == 0:
                    ls_nic_cmd = ("ip route | grep default | grep -v %s " +
                                  "| tr -s ' ' | cut -d ' ' -f 5") % self.nic
                    ls_nic = subprocess.Popen(ls_nic_cmd,
                                              shell=True,
                                              stdout=subprocess.PIPE
                                              ).stdout.read()
                    logger.debug("ls_nic => %s" % ls_nic)
                    for nic in ls_nic.split("\n"):
                        if nic:
                            ip_cmd = ("ip route | grep %s " +
                                      "| awk '/default/ { print $3 }'") % nic
                            ip = subprocess.Popen(ip_cmd, shell=True,
                                                  stdout=subprocess.PIPE
                                                  ).stdout.read()
                            subprocess.call("ip route del default via %s" % ip,
                                            shell=True)
                time.sleep(5)

            except Exception:
                logger.error("Error on monitoring")
                if not self.terminate():
                    continue


def delete_path(file_path):
    # remove file_path
    path_list = [file_path]
    if type(file_path) is list:
        path_list = file_path
    for p in path_list:
        try:
            os.unlink(p)
        except OSError:
            if os.path.exists(p):
                raise


def resolve_version():
    if 'VERSION' in os.environ:
        return os.environ['VERSION']
    return 'N/A'


def candy_command(category, action, serial_port, baudrate,
                  sock_path='/var/run/candy-board-service.sock'):
    delete_path(sock_path)
    atexit.register(delete_path, sock_path)

    serial = candy_board_qws.SerialPort(serial_port, baudrate)
    server = candy_board_qws.SockServer(resolve_version(),
                                        sock_path, serial)
    args = {}
    try:
        args = json.loads(action)
    except ValueError:
        args['action'] = action
    args['category'] = category
    ret = server.perform(args)
    logger.debug("candy_command() : %s:%s => %s" %
                 (category, args['action'], ret))
    print(ret)
    sys.exit(json.loads(ret)['status'] != 'OK')


def blinky():
    global led, led_sec, online
    if not online:
        led = 1
    led = 0 if led != 0 else 1
    if led == 0:
        subprocess.call("echo %d > /sys/class/gpio/%s/value" % (led, LED),
                        shell=True, stdout=Monitor.FNULL,
                        stderr=subprocess.STDOUT)
        threading.Timer(led_sec, blinky, ()).start()
    else:
        subprocess.call("echo %d > /sys/class/gpio/%s/value" % (1, LED),
                        shell=True, stdout=Monitor.FNULL,
                        stderr=subprocess.STDOUT)
        time.sleep(led_sec / 3)
        subprocess.call("echo %d > /sys/class/gpio/%s/value" % (0, LED),
                        shell=True, stdout=Monitor.FNULL,
                        stderr=subprocess.STDOUT)
        time.sleep(led_sec / 3)
        subprocess.call("echo %d > /sys/class/gpio/%s/value" % (1, LED),
                        shell=True, stdout=Monitor.FNULL,
                        stderr=subprocess.STDOUT)
        threading.Timer(led_sec / 3, blinky, ()).start()


def server_main(serial_port, bps, nic,
                sock_path='/var/run/candy-board-service.sock'):

    if os.path.isfile(PIDFILE):
        logger.error("server_main module is aleady running")
        sys.exit(1)
    file(PIDFILE, 'w').write(PID)
    delete_path(sock_path)

    logger.debug("server_main() : Setting up SerialPort...")
    serial = candy_board_qws.LazySerialPort(serial_port, bps)
    logger.debug("server_main() : Setting up SockServer...")
    server = candy_board_qws.SockServer(resolve_version(),
                                        sock_path, serial)

    if 'BLINKY' in os.environ and os.environ['BLINKY'] == "1":
        logger.debug("server_main() : Starting blinky timer...")
        blinky()
    logger.debug("server_main() : Setting up Monitor...")
    monitor = Monitor(nic)
    logger.debug("server_main() : Setting up Pinger...")
    pinger = Pinger(PPP_PING_INTERVAL_SEC)

    logger.debug("server_main() : Starting SockServer...")
    server.start()
    logger.debug("server_main() : Starting Monitor...")
    monitor.start()
    logger.debug("server_main() : Starting Pinger...")
    pinger.start()

    logger.debug("server_main() : Joining Monitor thread into main...")
    monitor.join()
    logger.debug("server_main() : Joining Pinger thread into main...")
    pinger.join()
    logger.debug("server_main() : Joining SockServer thread into main...")
    server.join()


if __name__ == '__main__':
    if len(sys.argv) < 4:
        logger.error("The Network Interface isn't ready. " +
                     "Shutting down.")
    elif len(sys.argv) > 4:
        candy_command(
            sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    else:
        logger.info("serial_port:%s (%s bps), nic:%s" %
                    (sys.argv[1], sys.argv[2], sys.argv[3]))
        try:
            server_main(sys.argv[1], sys.argv[2], sys.argv[3])
        except KeyboardInterrupt:
            pass