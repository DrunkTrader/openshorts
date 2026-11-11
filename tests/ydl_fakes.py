"""A stand-in for ``yt_dlp.YoutubeDL``: records its options and writes tiny files
where the real one would, so the download code runs without a network."""
import os


class FakeDownloadError(Exception):
    pass


class FakeYDL:
    # Reset by the ``fake_ydl`` fixture before each test.
    instances = []
    info = {}
    fail_extract = staticmethod(lambda params: None)  # may raise for a given params dict
    fail_sections = {}                                # section number -> failures left

    def __init__(self, params=None):
        self.params = params or {}
        self.calls = []
        FakeYDL.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=True, process=True):
        self.calls.append("extract_info")
        FakeYDL.fail_extract(self.params)
        return dict(FakeYDL.info)

    def download(self, urls):
        raise AssertionError("the fake only serves process_ie_result")

    def _write(self, info, section):
        tmpl = self.params['outtmpl']
        name = (tmpl.replace('%(ext)s', 'mp4' if 'merge_output_format' in self.params else 'm4a')
                    .replace('%(section_number)s', str(section) if section is not None else 'NA'))
        with open(name, 'wb') as f:
            f.write(b'x' * 64)
        return name

    def process_ie_result(self, info, download=True):
        self.calls.append("process_ie_result")
        ranges = [{}]
        if 'download_ranges' in self.params:
            ranges = list(self.params['download_ranges'](info, self))
        done = []
        for chapter in ranges:
            section = chapter.get('index')
            if FakeYDL.fail_sections.get(section, 0) > 0:
                FakeYDL.fail_sections[section] -= 1
                if not self.params.get('ignoreerrors'):
                    raise FakeDownloadError(f"section {section} failed")
                continue
            done.append({'filepath': self._write(info, section), 'section_number': section})
        return {'requested_downloads': done, 'filepath': done[0]['filepath'] if done else None}
