"""One-off bench investigation: normal viewer plus bounded raw diagnostic evidence."""
import argparse
import json
from pathlib import Path
import sys
import threading
import webbrowser
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'array_rig' / 'm40623'))
import mod50_hardware as hw
from mod50_live import LiveNoiseViewer
from mod50_web import Dashboard, make_server

parser = argparse.ArgumentParser()
parser.add_argument('--label', required=True)
parser.add_argument('--seconds', type=float, default=170.)
args = parser.parse_args()
folder = Path(__file__).parent

class ObservedViewer(LiveNoiseViewer):
    def __init__(self, **kwargs):
        self.raw = []
        self.sample_count = 0
        self.header = None
        self.last_logged_second = -1
        super().__init__(**kwargs, rig_factory=self.make_rig)

    def make_rig(self, **kwargs):
        rig = hw.Mod50Rig(**kwargs)
        start, read = rig.device.start_stream, rig.device.read_stream
        def start_stream(**opts):
            self.header = start(**opts)
            return self.header
        def read_stream(**opts):
            data = read(**opts)
            if data is not None:
                self.raw.append(data.copy())
                self.sample_count += len(data)
                if self.sample_count >= args.seconds * self.header.actual_timer_hz:
                    self.stop()
            return data
        rig.device.start_stream, rig.device.read_stream = start_stream, read_stream
        return rig

    def _publish(self, *positional, **kwargs):
        super()._publish(*positional, **kwargs)
        state = self.status()
        second = int(state['elapsed_s'])
        if second != self.last_logged_second:
            self.last_logged_second = second
            frame = {k: state[k] for k in ('elapsed_s', 'first_ready_s', 'recording_start_s', 'deadline_fallback', 'ready')}
            frame['channels'] = state['settling'].get('channels', [])
            with (folder / (args.label + '-frames.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(frame, allow_nan=False) + '\n')

    def run(self):
        try:
            super().run()
        finally:
            state = self.status()
            (folder / (args.label + '-final.json')).write_text(json.dumps(state, indent=2, allow_nan=False), encoding='utf-8')
            if self.raw:
                metadata = {'application_revision': hw.APP_REVISION, 'simulate': self.simulate,
                    'sample_rate_hz': self.header.actual_timer_hz, 'range_code': hw.NOISE_RANGE,
                    'installed_positions': self.positions, 'box_side': self.box_side,
                    'map_sha256': hw.MAP_SHA256, 'diagnostic_only': True, 'state': state}
                np.savez_compressed(folder / (args.label + '-raw.npz'), converter_counts=np.concatenate(self.raw),
                    metadata_json=json.dumps(metadata, allow_nan=False))
            print('Evidence saved: ' + args.label, flush=True)

dashboard = Dashboard(live_factory=ObservedViewer)
server = make_server(dashboard)
done = threading.Event()
def watch():
    while not done.wait(1):
        if dashboard.should_shutdown():
            server.shutdown()
            return
threading.Thread(target=watch, daemon=True).start()
print('Eltec array dashboard: ' + server.dashboard_url, flush=True)
webbrowser.open(server.dashboard_url + 'live.html')
try:
    server.serve_forever(poll_interval=.2)
finally:
    dashboard.stop()
    if dashboard.live_worker:
        dashboard.live_worker.join()
    done.set()
    server.server_close()
