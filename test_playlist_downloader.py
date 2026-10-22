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
    def test_existing_songs_ignore_numbering_and_preserve_m3u_paths(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            root = Path(folder) / 'Playlist'
            root.mkdir()
            tracks = [p.Track(1, 'Song', ('Artist',), '', 30),
                      p.Track(2, 'Missing', ('Artist',), '', 30),
                      p.Track(3, 'Gül', ('Other',), '', 30)]
            old = root / '099 - Song - Artist.MP3'
            old.write_bytes(b'audio')
            (root / '04 - Gul - Other.mp3').write_bytes(b'audio')
            (root / '02 - Missing - Artist.mp3').touch()
            (root / '02 - Missing - Artist.mp3.part').write_bytes(b'partial')
            (root / 'Song - Someone else.mp3').write_bytes(b'audio')
            existing = p.existing_track_files(root, tracks, 2)
            self.assertEqual(set(existing), {1, 3})
            self.assertEqual(existing[1], old)
            playlist = p.write_m3u(root, tracks, 2, False, set(existing), existing)
            content = playlist.read_text(encoding='utf-8-sig')
            self.assertIn('Playlist\\099 - Song - Artist.MP3', content)
            self.assertIn('Playlist\\04 - Gul - Other.mp3', content)
            self.assertNotIn('Missing', content)
            self.assertEqual(old.read_bytes(), b'audio')

    def setUp(self):
        p.cached_album_cover.cache_clear()

    def test_resume_only_submits_missing_tracks(self):
        tracks = [p.Track(1, 'Existing', ('Artist',), '', 30), p.Track(2, 'Missing', ('Artist',), '', 30)]
        metadata = p.PlaylistMetadata(tracks, 2, 'test', 'Playlist')
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            output = Path(folder) / 'Playlist'
            output.mkdir()
            (output / '01 - Existing - Artist.mp3').write_bytes(b'audio')
            with patch.object(p, 'fetch_metadata_from_public_page', return_value=metadata), \
                 patch.object(p, 'find_ffmpeg_location', return_value='ffmpeg'), \
                 patch.object(p, 'download_track', return_value=(2, True, 'downloaded')) as download:
                self.assertEqual(p.main(['A' * 22, '-o', folder, '--no-update-check']), 0)
                self.assertEqual(download.call_count, 1)
                self.assertEqual(download.call_args.args[0], tracks[1])
            (output / '02 - Missing - Artist.mp3').write_bytes(b'audio')
            with patch.object(p, 'fetch_metadata_from_public_page', return_value=metadata), \
                 patch.object(p, 'find_ffmpeg_location', return_value=None), \
                 patch.object(p, 'download_track') as download:
                self.assertEqual(p.main(['A' * 22, '-o', folder, '--no-update-check']), 0)
                download.assert_not_called()

    def test_cover_upgrades_thumbnail_and_caches_album(self):
        buffer = io.BytesIO()
        Image.new('RGB', (640, 640), 'red').save(buffer, 'JPEG')
        url = 'https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228'
        response = Mock(status_code=200, content=buffer.getvalue())
        with patch.object(p.requests, 'get', return_value=response) as get:
            first = p.fetch_album_cover(url)
            self.assertEqual(p.fetch_album_cover(url), first)
        get.assert_called_once_with(url.replace('00001e02', '0000b273'), timeout=30)

    def test_cover_missing_large_variant_falls_back(self):
        buffer = io.BytesIO()
        Image.new('RGB', (300, 300), 'blue').save(buffer, 'JPEG')
        url = 'https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228'
        with patch.object(p.requests, 'get', side_effect=[Mock(status_code=404), Mock(status_code=200, content=buffer.getvalue())]) as get:
            p.fetch_album_cover(url)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], url)

    def test_cover_rate_limit_does_not_request_smaller_image(self):
        url = 'https://i.scdn.co/image/ab67616d00001e02ff9ca10b55ce82ae553c8228'
        response = Mock(status_code=429)
        response.raise_for_status.side_effect = RuntimeError('429 Too Many Requests')
        with patch.object(p.requests, 'get', return_value=response) as get:
            with self.assertRaisesRegex(RuntimeError, '429'):
                p.fetch_album_cover(url)
        get.assert_called_once()

    def test_name_matches_beat_popular_unrelated_results(self):
        track = p.Track(1, 'Silent River', ('Elena Marlow',), '', 180)
        unrelated = dict(id='popular', title='Another Song Official Audio Lyrics', channel='Famous Artist - Topic', duration=180, view_count=500_000_000)
        wrong_artist = dict(id='wrong-artist', title='Silent River Official Audio', channel='Famous Artist - Topic', duration=180)
        close = dict(id='close', title='Elena Marlo - Silent Rivers (Lyrics)', channel='Small uploader', duration=200)
        exact = dict(id='exact', title='Elena Marlow - Silent River', channel='Small uploader', duration=210)
        self.assertGreater(p.score_candidate(unrelated, track), p.score_candidate(exact, track))
        ranked = p.rank_youtube_entries([unrelated, wrong_artist, close, exact], track, 12)
        self.assertEqual([c.url.rsplit('=', 1)[-1] for c in ranked], ['exact', 'close'])

    def test_recording_type_follows_spotify_title(self):
        normal = dict(id='normal', title='Sena Sahin - Yarali', duration=205)
        acoustic = dict(id='acoustic', title='Yarali - Sena Sahin (Akustik)', duration=203, view_count=500_000_000)
        for suffix, expected in [('', 'normal'), (' (Acoustic)', 'acoustic'), (' - Accoustic Version', 'acoustic'), (' (Akustik)', 'acoustic')]:
            with self.subTest(suffix=suffix):
                track = p.Track(1, 'Yarali' + suffix, ('Sena Sahin',), '', 205)
                candidates = p.rank_youtube_entries([acoustic, normal], track, 12)
                self.assertTrue(candidates[0].url.endswith('=' + expected))
        # Still allow the other version when it is the only name match.
        self.assertTrue(p.rank_youtube_entries([acoustic], p.Track(1, 'Yarali', ('Sena Sahin',), '', 205), 12)[0].url.endswith('=acoustic'))

    def test_no_name_match_uses_legacy_ranking(self):
        track = p.Track(1, 'Silent River', ('Elena Marlow',), '', 180)
        entries = [dict(id='a', title='Unrelated', channel='Someone', duration=240),
                   dict(id='b', title='Different Official Audio', channel='Other - Topic', duration=180)]
        ranked = p.rank_youtube_entries(entries, track, 12)
        expected = max(entries, key=lambda entry: p.score_candidate(entry, track))
        self.assertTrue(ranked[0].url.endswith('=' + expected['id']))

    def test_names_handle_accents_and_whole_tokens(self):
        self.assertEqual(p.name_similarity('G\u00fcl\u0131', 'Guli (Official Audio)'), 1)
        self.assertLess(p.name_similarity('Air', 'Fairground'), 0.82)
        self.assertEqual(p.name_similarity('\u041c\u0438\u0440', '\u041c\u0438\u0440 Lyrics'), 1)

    def test_search_scans_all_queries_with_bounded_results(self):
        track = p.Track(1, 'Silent River', ('Elena Marlow',), '', 180)
        unrelated = dict(id='a', title='Different Official Audio', channel='Other - Topic', duration=180)
        match = dict(id='b', title='Elena Marlow - Silent River', duration=190)
        ydl = Mock()
        ydl.extract_info.side_effect = [{'entries': [unrelated]}, {'entries': [match]}]
        context = Mock()
        context.__enter__ = Mock(return_value=ydl)
        context.__exit__ = Mock(return_value=False)
        with patch.object(p, 'YoutubeDL', return_value=context), patch.object(p, 'youtube_queries', return_value=['first', 'second']):
            ranked = p.search_youtube_candidates(track, 12)
        self.assertTrue(ranked[0].url.endswith('=b'))
        self.assertEqual(ydl.extract_info.call_count, 2)
        self.assertEqual(ydl.extract_info.call_args_list[1].args[0], 'ytsearch12:second')

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
                self.assertEqual(p.load_config(), dict(include_album_art=True, ascii_names=False, create_m3u=False, sort_by_artist=False))
                args = argparse.Namespace(**p.load_config())
                with patch('builtins.input', side_effect=['3', '4', '']):
                    p.config_prompt(args)
                self.assertTrue(p.load_config()['create_m3u'])
                self.assertTrue(p.load_config()['sort_by_artist'])
            output = root / 'My Playlist'
            output.mkdir()
            tracks = [p.Track(1, 'G\u00fcl\u0131', ('Artist',), '', 30), p.Track(2, 'Other', ('Artist',), '', 30)]
            path = Path(p.output_template(output, tracks[0], 2, True).replace('%(ext)s', 'mp3'))
            self.assertEqual(path.name, '01 - Guli - Artist.mp3')
            path.touch()
            playlist = p.write_m3u(output, tracks, 2, True, {1})
            self.assertEqual(playlist, root / 'My Playlist.m3u')
            self.assertEqual(playlist.read_bytes(), ('#EXTM3U\r\n#EXTENC:UTF-8\r\n#PLAYLIST:My Playlist\r\n#EXTINF:30,Guli - Artist\r\nMy Playlist\\01 - Guli - Artist.mp3\r\n').encode('utf-8-sig'))

    def test_artist_order_preserves_ties_and_renumbers(self):
        tracks = [p.Track(1, 'First', ('Zulu',), '', 30),
                  p.Track(2, 'Second', ('alpha',), '', 30),
                  p.Track(3, 'Third', ('Alpha',), '', 30)]
        self.assertEqual(p.order_tracks(tracks, False), tracks)
        ordered = p.order_tracks(tracks, True)
        self.assertEqual([t.title for t in ordered], ['Second', 'Third', 'First'])
        self.assertEqual([t.index for t in ordered], [1, 2, 3])
        self.assertEqual([t.index for t in tracks], [1, 2, 3])

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


