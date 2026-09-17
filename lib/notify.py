"""macOS 알림 — osascript 로만. 실패해도 봇을 멈추지 않는다."""
import subprocess


def _esc(s):
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def send(title, message, sound="Glass", enabled=True):
    if not enabled:
        return False
    script = 'display notification "%s" with title "%s"' % (
        _esc(str(message)[:240]), _esc(str(title)[:120]))
    if sound:
        script += ' sound name "%s"' % _esc(sound)
    try:
        result = subprocess.run(["osascript", "-e", script], check=False,
                                capture_output=True, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
