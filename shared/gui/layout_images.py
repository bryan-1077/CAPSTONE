"""Run-scoped implementation screenshots and capture provenance."""
from pathlib import Path
from PySide6.QtWidgets import QInputDialog, QLineEdit
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from .layout_gallery import LayoutGallery
from .physical_details import backend_report
from .verification_details import small_json


class DownloadSignals(QObject):
    finished = Signal(object, object)


class ImageDownload(QRunnable):
    def __init__(self, run, state, config, folder, stage):
        super().__init__()
        self.run_directory = run.directory
        self.stage = stage
        self.state, self.config, self.folder = state, config, folder
        self.signals = DownloadSignals()

    def run(self):
        try:
            from shared.remote.layout_images import collect_layout_images
            result = collect_layout_images(self.state, self.config, self.folder, stages=(self.stage,))
        except Exception as exc:
            result = {'status': 'unavailable',
                      'reason': f'Image download failed ({type(exc).__name__}).'}
        finally:
            self.config.clear()
        self.signals.finished.emit(self.run_directory, result)


class LayoutImages(LayoutGallery):
    images_available = Signal()

    def __init__(self):
        super().__init__()
        self.download_requested.connect(self.download_images)
        self._signature = None
        self._downloads = {}
        self._run = None
        self._report = {}
        self._folder = None

    def display(self, run):
        path, report = backend_report(run)
        folder = path.parent / 'backend_reports/layout_images' if path else None
        changed_run = self._run is None or self._run.directory != run.directory
        self._run, self._report, self._folder = run, report, folder
        if changed_run:
            if self.viewer is not None:
                self.viewer.close()
        receipt = small_json(folder / 'collection.json') if folder else {}
        build = report.get('outputs', {}).get('gdsii_remote_dir')
        images = []
        records = {}
        for stage in ('floorplan', 'placement', 'routing'):
            image = folder / (stage + '.png') if folder else None
            record = next((entry for entry in receipt.get('images', [])
                           if isinstance(entry, dict) and entry.get('stage') == stage), {})
            records[stage.capitalize()] = dict(record)
            if build:
                records[stage.capitalize()].setdefault('source_build', build)
            if image and image.is_file() and (not receipt or record.get('status') == 'available'):
                images.append((stage.capitalize(), image, record))
        signature = (run.directory, build, receipt, [(title, str(image), image.stat().st_mtime_ns)
                                             for title, image, _ in images])
        if signature == self._signature:
            self.update_download_controls()
            return
        self._signature = signature
        had_images = bool(self.images) and not changed_run
        self.set_images(images, reset=changed_run, records=records)
        self.update_download_controls()
        if images and not had_images:
            self.images_available.emit()

    def update_download_controls(self):
        job = self._downloads.get(self._run.directory) if self._run else None
        ready = bool(self._folder and self._report.get('outputs', {}).get('gdsii_remote_dir')
                     and self._report.get('ssh_host') and self._report.get('ssh_user'))
        for title, card in self.cards.items():
            card.download.setEnabled(ready and job is None)
            card.download.setToolTip('Download the ' + title.lower() + ' image from the server' if ready and job is None
                                    else 'Wait for the current image download to finish' if job
                                    else 'Server details are unavailable for this saved run')
            if job and job.stage == title.lower():
                card.set_status('Downloading ' + title.lower() + '…')

    def download_images(self, stage):
        card = self.cards.get(stage.capitalize())
        if self._run is None or card is None or not card.download.isEnabled():
            return
        report = self._report
        password, accepted = QInputDialog.getText(
            self, 'Download ' + stage.capitalize() + ' Image',
            f'Password for {report["ssh_user"]}@{report["ssh_host"]}\n'
            'Leave blank to use your SSH key. The password is not saved.',
            QLineEdit.EchoMode.Password)
        if not accepted:
            return
        config = {'host': report['ssh_host'], 'username': report['ssh_user'],
                  'port': report.get('ssh_port') or 22,
                  'password': password or None, 'key_filename': report.get('ssh_key_file')}
        if config['password'] and config['key_filename'] and not Path(config['key_filename']).expanduser().is_file():
            config['key_filename'] = None
        job = ImageDownload(self._run, report, config, self._folder, stage)
        job.signals.finished.connect(self.download_finished)
        self._downloads[self._run.directory] = job
        self.update_download_controls()
        QThreadPool.globalInstance().start(job)

    def download_finished(self, directory, receipt):
        job = self._downloads.pop(directory, None)
        if self._run is None or self._run.directory != directory or job is None:
            return
        self._signature = None
        self.display(self._run)
        card = self.cards.get(job.stage.capitalize())
        if card is not None:
            record = next((entry for entry in receipt.get('images', []) if entry.get('stage') == job.stage), {})
            if job.stage in receipt.get('downloaded_stages', []):
                message = job.stage.capitalize() + ' downloaded.'
            else:
                message = receipt.get('reason') or record.get('reason') or 'This image could not be downloaded.'
            card.set_status(message)
