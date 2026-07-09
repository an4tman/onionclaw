import os
import sys
import time
import socket
import glob
import datetime

# Configuration — read from soc-suite.env values (exported into the environment),
# with the suite's documented defaults as fallback. No site details are hardcoded.
SO_IP = os.environ.get("SOC_SO_IP", "")
SO_PORT = int(os.environ.get("SOC_SYSLOG_PORT", "514"))
PIHOLE_LOG = os.environ.get("SOC_PIHOLE_LOG", "")
OPENCLAW_LOG_DIR = os.environ.get("SOC_OPENCLAW_LOG_DIR", "")

if not SO_IP:
    sys.exit("soc-log-forwarder: SOC_SO_IP is unset — source your soc-suite.env first.")

def log_internal(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [internal] {msg}")
    sys.stdout.flush()

class FileTailer:
    def __init__(self, filepath, tag, parse_func=None):
        self.filepath = filepath
        self.tag = tag
        self.parse_func = parse_func
        self.file = None
        self.last_inode = None

    def open_file(self, seek_to_end=True):
        if os.path.exists(self.filepath):
            try:
                inode = os.stat(self.filepath).st_ino
                self.file = open(self.filepath, "r", errors="ignore")
                if seek_to_end:
                    self.file.seek(0, 2)  # Go to end
                else:
                    self.file.seek(0, 0)  # Go to beginning
                self.last_inode = inode
                log_internal(f"Started tailing {self.filepath} (inode: {inode})")
                return True
            except Exception as e:
                log_internal(f"Error opening {self.filepath}: {e}")
        return False

    def check_rotation(self):
        if not os.path.exists(self.filepath):
            if self.file:
                self.file.close()
                self.file = None
                log_internal(f"File vanished: {self.filepath}")
            return

        try:
            stat_res = os.stat(self.filepath)
            current_inode = stat_res.st_ino
            
            is_truncated = False
            if self.file:
                pos = self.file.tell()
                if stat_res.st_size < pos:
                    is_truncated = True

            if is_truncated or current_inode != self.last_inode or self.file is None:
                log_internal(f"Rotation or truncation detected for {self.filepath}")
                if self.file:
                    self.file.close()
                self.open_file(seek_to_end=False)
        except Exception as e:
            log_internal(f"Error checking rotation for {self.filepath}: {e}")

    def read_lines(self):
        if not self.file:
            self.open_file()
            return []

        lines = []
        while True:
            line = self.file.readline()
            if not line:
                break
            lines.append(line.strip())
        return lines

def format_pihole(line):
    # pihole.log lines already start with standard syslog timestamp, e.g.:
    # Jun  3 16:00:54 dnsmasq[12345]: query[A] ...
    # We just prepend priority <30> (daemon.info)
    return f"<30>{line}"

def format_openclaw(line):
    # OpenClaw logs look like:
    # [2026-06-03 16:00:54.123] [info] [gateway] message
    # We parse the timestamp and convert to syslog format
    # If parsing fails, we use current time.
    try:
        if line.startswith("[") and "]" in line:
            parts = line.split("]", 1)
            ts_str = parts[0][1:].split(".")[0] # strip ms
            dt = datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            ts_syslog = dt.strftime("%b %e %H:%M:%S")
            msg = parts[1].strip()
        else:
            ts_syslog = datetime.datetime.now().strftime("%b %e %H:%M:%S")
            msg = line
    except Exception:
        ts_syslog = datetime.datetime.now().strftime("%b %e %H:%M:%S")
        msg = line

    # Priority <14> (user.info), tag OpenClaw
    return f"<14>{ts_syslog} OpenClaw: {msg}"

def get_latest_openclaw_log():
    files = glob.glob(os.path.join(OPENCLAW_LOG_DIR, "openclaw-*.log"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

def main():
    log_internal("Starting SOC Log Forwarder daemon...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    pihole_tailer = FileTailer(PIHOLE_LOG, "pihole", format_pihole)
    
    openclaw_filepath = get_latest_openclaw_log()
    openclaw_tailer = FileTailer(openclaw_filepath, "openclaw", format_openclaw) if openclaw_filepath else None

    last_rotation_check = time.time()

    while True:
        try:
            now = time.time()
            if now - last_rotation_check > 5:
                # Check rotation for PiHole
                pihole_tailer.check_rotation()
                
                # Check rotation/new file for OpenClaw
                latest_oc = get_latest_openclaw_log()
                if latest_oc:
                    if not openclaw_tailer:
                        openclaw_tailer = FileTailer(latest_oc, "openclaw", format_openclaw)
                    elif openclaw_tailer.filepath != latest_oc:
                        log_internal(f"Switching OpenClaw tail target to {latest_oc}")
                        if openclaw_tailer.file:
                            openclaw_tailer.file.close()
                        openclaw_tailer = FileTailer(latest_oc, "openclaw", format_openclaw)
                    openclaw_tailer.check_rotation()
                
                last_rotation_check = now

            # Process PiHole
            for line in pihole_tailer.read_lines():
                if line:
                    syslog_msg = pihole_tailer.parse_func(line)
                    sock.sendto(syslog_msg.encode("utf-8"), (SO_IP, SO_PORT))

            # Process OpenClaw
            if openclaw_tailer:
                for line in openclaw_tailer.read_lines():
                    if line:
                        syslog_msg = openclaw_tailer.parse_func(line)
                        sock.sendto(syslog_msg.encode("utf-8"), (SO_IP, SO_PORT))

            time.sleep(0.5)

        except KeyboardInterrupt:
            log_internal("Stopping SOC Log Forwarder daemon...")
            break
        except Exception as e:
            log_internal(f"Main loop error: {e}")
            time.sleep(2)

if __name__ == "__main__":
    main()
