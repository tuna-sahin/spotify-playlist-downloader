import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from PIL import Image
from mutagen.id3 import ID3
import playlist_downloader as p

class DownloaderTests(unittest.TestCase):
    def test_artwork_and_tags_preserve_audio(self):
        image = Image.effect_noise((900, 700), 100).convert('RGB')
        source = io.BytesIO()
        image.save(source, 'PNG')
        jpeg = p.prepare_cover(source.getvalue())
        self.assertLess(len(jpeg), 100000)
        with Image.open(io.BytesIO(jpeg)) as cover:
            self.assertEqual(cover.size, (500, 500))
            self.assertEqual(cover.mode, 'RGB')
            self.assertEqual(cover.format, 'JPEG')
            self.assertFalse(cover.info.get('progressive'))
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            path = Path(folder) / 'test.mp3'
            audio = b'\xff\xfb' + bytes(range(256)) * 20
            path.write_bytes(audio)
            response = Mock(content=source.getvalue())
            with patch.object(p, 'requests', Mock(get=Mock(return_value=response))):
                p.write_mp3_tags(path, p.Track(1, 'G\u00fcl', ('A',), 'Album', 30, 'https://i.scdn.co/image/test'), True, False)
            tags = ID3(path)
            self.assertEqual(tags.version, (2, 3, 0))
            self.assertEqual(tags['TIT2'].text, ['G\u00fcl'])
            self.assertTrue(all(f.encoding == 1 for f in tags.values() if hasattr(f, 'encoding')))
            cover = tags.getall('APIC')[0]
            self.assertEqual(cover.type, 3)
            self.assertEqual(cover.mime, 'image/jpeg')
            self.assertTrue(path.read_bytes().endswith(audio))
            with patch.object(p, 'requests', Mock()) as requests_mock:
                p.write_mp3_tags(path, p.Track(1, 'G\u00fcl', ('A',), '', 30), False, True)
                requests_mock.get.assert_not_called()
            self.assertFalse(ID3(path).getall('APIC'))
            self.assertEqual(ID3(path)['TIT2'].text, ['Gul'])

    def test_config_and_m3u(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder)
            with patch.object(p, 'app_base_dir', return_value=root):
                self.assertEqual(p.load_config(), dict(include_album_art=True, ascii_names=False, create_m3u=False))
                args = argparse.Namespace(**p.load_config())
                with patch('builtins.input', side_effect=['3', '']):
                    p.config_prompt(args)
                self.assertTrue(p.load_config()['create_m3u'])
            output = root / 'My Playlist'
            output.mkdir()
            tracks = [p.Track(1, 'G\u00fcl\u0131', ('Artist',), '', 30), p.Track(2, 'Other', ('Artist',), '', 30)]
            path = Path(p.output_template(output, tracks[0], 2, True).replace('%(ext)s', 'mp3'))
            self.assertEqual(path.name, '01 - Guli - Artist.mp3')
            path.touch()
            playlist = p.write_m3u(output, tracks, 2, True, {1})
            self.assertEqual(playlist, root / 'My Playlist.m3u')
            self.assertEqual(playlist.read_bytes(), ('#EXTM3U\r\n#EXTENC:UTF-8\r\n#PLAYLIST:My Playlist\r\n#EXTINF:30,Guli - Artist\r\nMy Playlist\\01 - Guli - Artist.mp3\r\n').encode('utf-8-sig'))

    def test_m3u_matches_device_example(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            output = Path(folder) / 'farazi_v_kayra'
            output.mkdir()
            track = p.Track(1, 'Yang\u0131n I: Sakin', ('Farazi V Kayra',), '', 156)
            Path(p.output_template(output, track, 2).replace('%(ext)s', 'mp3')).touch()
            playlist = p.write_m3u(output, [track], 2, False, {1})
            expected = ('#EXTM3U\r\n#EXTENC:UTF-8\r\n#PLAYLIST:farazi_v_kayra\r\n'
                        '#EXTINF:156,Yang\u0131n I_ Sakin - Farazi V Kayra\r\n'
                        'farazi_v_kayra\\01 - Yang\u0131n I_ Sakin - Farazi V Kayra.mp3\r\n')
            self.assertEqual(playlist.read_bytes(), expected.encode('utf-8-sig'))

if __name__ == '__main__':
    unittest.main()


