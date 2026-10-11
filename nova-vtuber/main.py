"""Nova VTuber — point d'entrée."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GLOG_minloglevel", "2")          # moins de messages MediaPipe dans la console
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--ignore-gpu-blocklist")


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s : %(message)s")
    fh = RotatingFileHandler(ROOT / "logs" / "nova.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])

    def hook(t, v, tb):
        logging.getLogger("nova").critical("Erreur non gérée", exc_info=(t, v, tb))
    sys.excepthook = hook


def main():
    setup_logging()
    import setup_assets
    missing = setup_assets.missing_assets()
    if missing:
        logging.info("Ressources manquantes, téléchargement : %s", missing)
        setup_assets.main()
    from PySide6.QtWidgets import QApplication, QMessageBox
    from src.ui.main_window import MainWindow
    app = QApplication(sys.argv)
    app.setApplicationName("Nova VTuber")
    app.setQuitOnLastWindowClosed(False)        # Nova peut continuer dans la zone de notification
    # une seule instance : si Nova est déjà ouvert, on l'affiche au lieu d'en lancer un deuxième
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    key = "NovaVTuber-" + os.environ.get("USERNAME", "user")
    testing = "--smoke-test" in sys.argv
    if not testing:
        sock = QLocalSocket()
        sock.connectToServer(key)
        if sock.waitForConnected(500):
            sock.write(b"show")
            sock.flush()
            sock.waitForBytesWritten(500)
            logging.info("Nova est déjà ouvert : fenêtre existante affichée")
            return 0
    if setup_assets.missing_assets():
        QMessageBox.critical(None, "Nova VTuber", "Ressources manquantes (connexion Internet requise au premier lancement).\n"
                             "Relancez install.bat.")
        return 1
    win = MainWindow()
    win.show()
    if not testing:
        server = QLocalServer(win)
        QLocalServer.removeServer(key)
        server.listen(key)
        server.newConnection.connect(lambda: (server.nextPendingConnection(), win.bring_to_front()))
    if testing:   # démarrage + fermeture automatique (tests)
        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(os.environ.get("NOVA_SMOKE_MS", "6000")), win.quit_app)
    return app.exec()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
