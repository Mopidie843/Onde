package fr.onde.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageInstaller;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.security.MessageDigest;

/**
 * Mise à jour de l'appli depuis le serveur Onde : au démarrage (puis au plus toutes les 6 h), l'appli
 * demande au serveur quelle version d'APK il fournit. Plus récente → proposition, téléchargement
 * (connexion vérifiée comme le reste : certificat épinglé), empreinte SHA-256 contrôlée, puis
 * installation par Android (qui demande confirmation). Même signature : tes réglages sont gardés.
 */
final class Updater {
    static final String ACTION = "fr.onde.app.INSTALL_STATUS";
    private static final long EVERY = 6L * 3600 * 1000;
    private static long lastCheck;
    private static volatile boolean busy;
    private static File pending;

    private Updater() {}

    static int installedCode(Activity a) {
        try { return a.getPackageManager().getPackageInfo(a.getPackageName(), 0).versionCode; }
        catch (Exception e) { return 0; }
    }

    /** Au chargement d'Onde : nouvelle appli disponible sur le serveur ? */
    static void maybeCheck(final Activity a, final String base) {
        long now = System.currentTimeMillis();
        if (busy || base == null || now - lastCheck < EVERY) return;
        lastCheck = now;
        new Thread(new Runnable() {
            @Override public void run() {
                Net.Reply r = Net.call(base + "/api/app/version", "GET", null, null);
                if (r.code != 200) return;
                try {
                    JSONObject apk = new JSONObject(r.body).optJSONObject("apk");
                    if (apk == null) return;
                    final int code = apk.optInt("versionCode", 0);
                    final String name = apk.optString("versionName", "");
                    final String sha = apk.optString("sha256", "");
                    if (code <= installedCode(a) || sha.length() != 64) return;
                    if (Prefs.sp(a).getInt("upd_later", 0) == code
                            && now() - Prefs.sp(a).getLong("upd_later_at", 0) < 24L * 3600 * 1000) return;
                    a.runOnUiThread(new Runnable() { @Override public void run() { offer(a, base, code, name, sha); } });
                } catch (Exception ignored) { }
            }
        }, "onde-update-check").start();
    }

    private static long now() { return System.currentTimeMillis(); }

    private static void offer(final Activity a, final String base, final int code, String name, final String sha) {
        if (a.isFinishing()) return;
        new AlertDialog.Builder(a, android.R.style.Theme_DeviceDefault_Dialog_Alert)
            .setTitle("Mise à jour d'Onde")
            .setMessage("La version " + (name.isEmpty() ? String.valueOf(code) : name) + " de l'appli est prête sur ton serveur.\n\n"
                    + "Elle s'installe par-dessus : tes réglages et ta musique téléchargée sont gardés.")
            .setPositiveButton("Mettre à jour", new android.content.DialogInterface.OnClickListener() {
                @Override public void onClick(android.content.DialogInterface d, int w) { download(a, base, sha); }
            })
            .setNegativeButton("Plus tard", new android.content.DialogInterface.OnClickListener() {
                @Override public void onClick(android.content.DialogInterface d, int w) {
                    Prefs.sp(a).edit().putInt("upd_later", code).putLong("upd_later_at", now()).apply();
                }
            })
            .show();
    }

    private static void download(final Activity a, final String base, final String sha) {
        if (busy) return;
        busy = true;
        Toast.makeText(a, "Téléchargement de la mise à jour…", Toast.LENGTH_SHORT).show();
        new Thread(new Runnable() {
            @Override public void run() {
                File f = new File(a.getCacheDir(), "onde-update.apk");
                String err = null;
                HttpURLConnection c = null;
                try {
                    c = Net.open(base + "/api/app/apk", 8000, 30000);
                    if (c.getResponseCode() != 200) throw new Exception("le serveur a répondu " + c.getResponseCode());
                    MessageDigest md = MessageDigest.getInstance("SHA-256");
                    InputStream in = c.getInputStream();
                    OutputStream out = new FileOutputStream(f);
                    byte[] buf = new byte[65536];
                    int n;
                    long total = 0;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n); md.update(buf, 0, n); total += n;
                        if (total > 80L * 1024 * 1024) throw new Exception("fichier trop gros");
                    }
                    out.close(); in.close();
                    StringBuilder hex = new StringBuilder();
                    for (byte b : md.digest()) hex.append(String.format("%02x", b));
                    if (!hex.toString().equalsIgnoreCase(sha)) throw new Exception("empreinte différente (fichier abîmé)");
                } catch (Exception e) {
                    err = e.getMessage();
                    f.delete();
                } finally {
                    if (c != null) c.disconnect();
                    busy = false;
                }
                final String error = err;
                final File file = f;
                a.runOnUiThread(new Runnable() {
                    @Override public void run() {
                        if (error != null) Toast.makeText(a, "Mise à jour impossible : " + error, Toast.LENGTH_LONG).show();
                        else install(a, file);
                    }
                });
            }
        }, "onde-update-dl").start();
    }

    /** Installe l'APK téléchargé (Android demande à l'utilisateur de confirmer). */
    static void install(Activity a, File f) {
        if (Build.VERSION.SDK_INT >= 26 && !a.getPackageManager().canRequestPackageInstalls()) {
            pending = f;
            Toast.makeText(a, "Autorise Onde à installer sa mise à jour, puis reviens dans l'appli.", Toast.LENGTH_LONG).show();
            try {
                a.startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:" + a.getPackageName())));
            } catch (Exception ignored) { }
            return;
        }
        pending = null;
        try {
            PackageInstaller pi = a.getPackageManager().getPackageInstaller();
            PackageInstaller.SessionParams p = new PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL);
            p.setAppPackageName(a.getPackageName());
            int id = pi.createSession(p);
            PackageInstaller.Session s = pi.openSession(id);
            OutputStream out = s.openWrite("onde.apk", 0, f.length());
            InputStream in = new FileInputStream(f);
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            in.close();
            s.fsync(out);
            out.close();
            Intent i = new Intent(a, MainActivity.class).setAction(ACTION);
            int flags = PendingIntent.FLAG_UPDATE_CURRENT | (Build.VERSION.SDK_INT >= 31 ? 0x02000000 /* FLAG_MUTABLE */ : 0);
            PendingIntent done = PendingIntent.getActivity(a, id, i, flags);
            s.commit(done.getIntentSender());
            s.close();
        } catch (Exception e) {
            Toast.makeText(a, "Installation impossible : " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    /** Retour des réglages « installer des applis inconnues » : on reprend l'installation. */
    static void onResume(Activity a) {
        if (pending != null && (Build.VERSION.SDK_INT < 26 || a.getPackageManager().canRequestPackageInstalls())) install(a, pending);
    }

    /** Réponse d'Android à l'installation. Renvoie true si l'intent était pour nous. */
    static boolean onStatus(Activity a, Intent intent) {
        if (intent == null || !ACTION.equals(intent.getAction())) return false;
        int st = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, -999);
        if (st == PackageInstaller.STATUS_PENDING_USER_ACTION) {
            Intent confirm = intent.getParcelableExtra(Intent.EXTRA_INTENT);
            if (confirm != null) {
                try { a.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); } catch (Exception ignored) { }
            }
        } else if (st != PackageInstaller.STATUS_SUCCESS) {
            String msg = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE);
            if (st != PackageInstaller.STATUS_FAILURE_ABORTED)
                Toast.makeText(a, "Mise à jour non installée" + (msg != null ? " : " + msg : ""), Toast.LENGTH_LONG).show();
        }
        return true;
    }
}
