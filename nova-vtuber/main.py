"""Nova VTuber — point d'entrée."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GLOG_minloglevel", "2")          # moins de messages MediaPipe dans la console
# moteur d'affichage de l'avatar : pas de pause ni de ralentissement quand un jeu recouvre la fenêtre
from src import perf  # noqa: E402

os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = perf.chromium_flags(
    os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "--ignore-gpu-blocklist"))


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


TRAY_MENU_CSS = ("QMenu { background: #ffffff; color: #000000; border: 1px solid #c8c8d0; padding: 4px; } "
                 "QMenu::item { padding: 6px 22px; background: transparent; color: #000000; } "
                 "QMenu::item:selected { background: #e6e1ff; color: #000000; } "
                 "QMenu::item:disabled { color: #8a8a8a; } "
                 "QMenu::separator { height: 1px; background: #dcdce4; margin: 4px 8px; }")


def fix_tray_menu(app, win):
    """Menu de l'icône près de l'horloge (Nova en arrière-plan) : texte noir sur fond clair. Le thème sombre de
    Nova mettait ce texte en blanc alors que le menu garde le fond clair de Windows : il était illisible."""
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QMenu, QSystemTrayIcon

    class TrayMenuFix(QObject):
        def eventFilter(self, obj, ev):
            if ev.type() == QEvent.Type.Show and isinstance(obj, QMenu):
                trays = [t.contextMenu() for t in win.findChildren(QSystemTrayIcon)]
                if (obj in trays or obj.parentWidget() is None) and obj.styleSheet() != TRAY_MENU_CSS:
                    obj.setStyleSheet(TRAY_MENU_CSS)
            return False

    app.nova_tray_fix = TrayMenuFix(app)
    app.installEventFilter(app.nova_tray_fix)


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
    try:
        perf.boost(True)        # anti-bridage dès le lancement (l'interface peut le désactiver)
    except Exception as e:  # noqa: BLE001
        logging.warning("Anti-bridage indisponible : %s", e)
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
    fix_tray_menu(app, win)
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
