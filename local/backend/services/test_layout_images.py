import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from services.layout_images import collect_layout_images


class CollectionTests(unittest.TestCase):
    def test_one_image_download_preserves_other_downloaded_stages(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shared.remote.layout_images.SSHExecutor') as executor:
            folder = Path(tmp)
            (folder / 'floorplan.png').write_bytes(b'original floorplan')
            (folder / 'collection.json').write_text(json.dumps({
                'remote_build': '/build', 'images': [{'stage': 'floorplan', 'status': 'available'}]}))
            def fetch(remote, local, **kwargs):
                if remote.endswith('manifest.json'):
                    Path(local).write_text(json.dumps({'images': [
                        {'stage': stage, 'status': 'success'} for stage in ('floorplan', 'placement', 'routing')]}))
                else:
                    Path(local).write_bytes(b'new routing')
                return {'ok': True}
            executor.return_value.fetch_file.side_effect = fetch
            result = collect_layout_images({'outputs': {'gdsii_remote_dir': '/build'}}, {}, folder, stages=('routing',))
            self.assertEqual(result['downloaded_stages'], ['routing'])
            self.assertEqual([call.args[0] for call in executor.return_value.fetch_file.call_args_list], [
                '/build/results/screenshots/manifest.json', '/build/results/screenshots/routing.png'])
            self.assertEqual((folder / 'floorplan.png').read_bytes(), b'original floorplan')
            self.assertEqual({item['stage'] for item in result['images']}, {'floorplan', 'routing'})

    def test_backend_completion_downloads_with_current_credentials_and_records_result(self):
        import app
        state = {'stage_status': {'gdsii': 'success'}, 'current_stage': 'done',
                 'outputs': {'gdsii_remote_dir': '/final/build'}}
        config = {'host': 'offline', 'username': 'test', 'password': 'test-only-password'}
        with tempfile.TemporaryDirectory() as tmp, patch(
                'services.layout_images.collect_layout_images',
                return_value={'status': 'available'}) as collect:
            app._download_layout_images(state, config, Path(tmp))
            collect.assert_called_once_with(state, config, Path(tmp) / 'layout_images')
            self.assertEqual(state['layout_images']['status'], 'available')
            self.assertEqual(state['current_stage'], 'done')
            self.assertNotIn(config['password'], json.dumps(state))
            state['stage_status']['gdsii'] = 'pending'
            app._download_layout_images(state, config, Path(tmp))
            self.assertEqual(collect.call_count, 1)

    def test_only_successful_captures_from_selected_build_are_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shared.remote.layout_images.SSHExecutor') as executor:
            def fetch(remote, local, **kwargs):
                if remote.endswith('manifest.json'):
                    Path(local).write_text(json.dumps({'images': [
                        {'stage': 'routing', 'status': 'success', 'file': '/other/build/wrong.png'},
                        {'stage': 'floorplan', 'status': 'failed', 'error': 'No display'}]}))
                    return {'ok': True}
                Path(local).write_bytes(b'png-fixture')
                return {'ok': True}
            executor.return_value.fetch_file.side_effect = fetch
            result = collect_layout_images({'outputs': {'gdsii_remote_dir': '/selected/build'}}, {}, tmp)
            self.assertEqual(result['status'], 'partial')
            calls = executor.return_value.fetch_file.call_args_list
            self.assertEqual([call.args[0] for call in calls], [
                '/selected/build/results/screenshots/manifest.json',
                '/selected/build/results/screenshots/routing.png'])
            self.assertTrue((Path(tmp) / 'collection.json').is_file())
            executor.return_value.close.assert_called_once()

    def test_connection_failure_is_recorded_without_changing_design_status(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shared.remote.layout_images.SSHExecutor') as executor:
            executor.return_value.connect.side_effect = OSError('offline')
            state = {'outputs': {'gdsii_remote_dir': '/build'}, 'stage_status': {'gdsii': 'success'}}
            result = collect_layout_images(state, {}, tmp)
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(state['stage_status']['gdsii'], 'success')
            executor.return_value.close.assert_called_once()

    def test_failed_retry_keeps_completed_image_and_does_not_publish_partial_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shared.remote.layout_images.SSHExecutor') as executor:
            folder = Path(tmp)
            (folder / 'routing.png').write_bytes(b'original')
            (folder / 'collection.json').write_text(json.dumps({
                'remote_build': '/build', 'images': [{'stage': 'routing', 'status': 'available'}]}))
            def fetch(remote, local, **kwargs):
                if remote.endswith('manifest.json'):
                    Path(local).write_text('{"images":[{"stage":"routing","status":"success"}]}')
                    return {'ok': True}
                Path(local).write_bytes(b'incomplete')
                return {'ok': False}
            executor.return_value.fetch_file.side_effect = fetch
            result = collect_layout_images({'outputs': {'gdsii_remote_dir': '/build'}}, {}, folder)
            self.assertEqual((folder / 'routing.png').read_bytes(), b'original')
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(next(entry for entry in result['images'] if entry['stage'] == 'routing')['status'], 'available')
            self.assertFalse(list(folder.glob('.download-*')))
