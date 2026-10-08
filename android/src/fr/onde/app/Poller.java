package fr.onde.app;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;

/**
 * Écoute les événements d'Onde (« quoi de neuf depuis le numéro N ? ») en attente longue : la réponse arrive dès
 * qu'il se passe quelque chose, le téléphone ne harcèle pas le Pi. Aucun serveur tiers (ni Google ni autre) :
 * l'appli parle directement à ton Onde, sur le Wi-Fi comme depuis Internet.
 * Un seul Poller par processus, qui vit tant que l'appli, la lecture ou le service permanent le garde éveillé.
 */
final class Poller implements Runnable {
    private static Poller instance;

    private final Context ctx;
    private volatile boolean stopped;
    private Thread thread;

    // état « serveur injoignable »
    private int failures;
    private long firstFailure;
    private String okNet;          // réseau (Wi-Fi/4G) sur lequel le serveur répondait en dernier
    private boolean downNotified;

    private Poller(Context c) { ctx = c.getApplicationContext(); }

    static synchronized void ensure(Context c) {
        if (instance != null && instance.thread != null && instance.thread.isAlive()) return;
        instance = new Poller(c);
        instance.thread = new Thread(instance, "onde-poller");
        instance.thread.setDaemon(true);
        instance.thread.start();
    }

    static synchronized void stop() {
        if (instance != null) {
            instance.stopped = true;
            if (instance.thread != null) instance.thread.interrupt();
            instance = null;
        }
    }

    // ------------------------------------------------------------------ boucle
    @Override public void run() {
        while (!stopped) {
            long wait = 1000;
            try {
                wait = cycle();
            } catch (Exception e) {
                wait = 15000;
            }
            if (!sleep(wait)) return;
        }
    }

    private boolean sleep(long ms) {
        try { Thread.sleep(ms); return true; } catch (InterruptedException e) { return false; }
    }

    /** Une attente longue ; renvoie la pause avant la suivante (ms). */
    private long cycle() throws Exception {
        if (!Prefs.get(ctx, Prefs.NOTIF, true)) return 30000;
        String base = Prefs.str(ctx, Prefs.ACTIVE);
        if (base == null) base = Prefs.str(ctx, Prefs.SERVER);
        if (base == null) base = Prefs.str(ctx, Prefs.REMOTE);
        if (base == null) return 20000;  // pas encore de serveur choisi

        long last = Prefs.sp(ctx).getLong("n_last", -1);
        String url = base + "/api/notifications?wait=25&after=" + (last < 0 ? "latest" : String.valueOf(last));
        HttpURLConnection c = null;
        try {
            c = Net.open(url, 6000, 40000);
            String cookie = CookieManager.getInstance().getCookie(base);
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            int code = c.getResponseCode();
            if (code == 401) { reachable(); return 60000; }       // mot de passe pas encore saisi dans l'appli
            if (code != 200) return failed(base, 30000);
            JSONObject o = new JSONObject(read(c));
            reachable();
            handle(o);
            return 300;
        } catch (java.io.IOException e) {
            return failed(base, 0);
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private static String read(HttpURLConnection c) throws java.io.IOException {
        InputStream in = c.getInputStream();
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n, total = 0;
            while ((n = in.read(buf)) > 0 && total < 256 * 1024) { out.write(buf, 0, n); total += n; }
            return out.toString("UTF-8");
        } finally {
            in.close();
        }
    }

    private void handle(JSONObject o) throws Exception {
        long last = o.optLong("last", -1);
        if (o.optBoolean("reset")) {  // première connexion, ou autre serveur : on repart d'ici, sans rejouer l'historique
            Prefs.sp(ctx).edit().putLong("n_last", last).apply();
            return;
        }
        JSONArray events = o.optJSONArray("events");
        long max = Prefs.sp(ctx).getLong("n_last", -1);
        if (events != null) {
            for (int i = 0; i < events.length(); i++) {
                JSONObject e = events.getJSONObject(i);
                Notifier.show(ctx, e.optString("kind"), e.optString("title", "Onde"), e.optString("text"), e.optString("link"));
                max = Math.max(max, e.optLong("id"));
            }
        }
        Prefs.sp(ctx).edit().putLong("n_last", Math.max(max, last)).apply();
    }

    // ------------------------------------------------------------------ serveur injoignable / de retour
    private String netKey() {
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkInfo ni = cm.getActiveNetworkInfo();
            if (ni == null || !ni.isConnected()) return null;
            return ni.getType() + "|" + ni.getExtraInfo();
        } catch (Exception e) {
            return null;
        }
    }

    private void reachable() {
        failures = 0;
        okNet = netKey();
        if (downNotified) {
            downNotified = false;
            Notifier.cancel(ctx, "down");
            if (Prefs.get(ctx, Prefs.DOWN, true)) Notifier.show(ctx, "up", "Onde est de retour", "Ton serveur répond de nouveau.", "");
        }
    }

    /** Échec de connexion. Prévient seulement si le téléphone est resté sur le MÊME réseau (le Pi est tombé),
     *  pas quand on a simplement quitté le Wi-Fi de la maison. Renvoie la pause avant le prochain essai. */
    private long failed(String base, long fixed) {
        long now = System.currentTimeMillis();
        if (failures == 0) firstFailure = now;
        failures++;
        if (failures == 2) {  // l'autre adresse (maison / à distance) répond peut-être : on bascule dessus
            String home = Prefs.str(ctx, Prefs.SERVER), remote = Prefs.str(ctx, Prefs.REMOTE);
            String alt = base.equals(home) ? remote : home;
            if (alt != null && !alt.equals(base) && Net.probe(alt, 2500, 4000) != null) {
                Prefs.put(ctx, Prefs.ACTIVE, alt);
                failures = 0;
                return 500;
            }
        }
        String key = netKey();
        if (key == null || okNet == null || !key.equals(okNet)) {
            // réseau perdu ou changé : normal, on ne prévient pas ; on réessaie plus calmement
            if (key != null && !key.equals(okNet)) okNet = null;
            failures = Math.min(failures, 3);
            return fixed > 0 ? fixed : Math.min(60000, 5000L * failures);
        }
        if (!downNotified && failures >= 3 && now - firstFailure >= 120000 && Prefs.get(ctx, Prefs.DOWN, true)) {
            downNotified = true;
            Notifier.show(ctx, "down", "Onde est injoignable",
                    "Le serveur ne répond plus alors que le réseau fonctionne : vérifie que le Pi est allumé.", "");
        }
        return fixed > 0 ? fixed : Math.min(60000, 5000L * failures);
    }
}
