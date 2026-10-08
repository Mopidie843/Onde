package fr.onde.app;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.MediaMetadata;
import android.media.session.MediaSession;
import android.media.session.PlaybackState;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.webkit.CookieManager;

import java.io.InputStream;
import java.net.HttpURLConnection;

/**
 * Lecture en arrière-plan : tant qu'Onde joue, ce service au premier plan garde l'appli (et le Wi-Fi)
 * éveillée, affiche la notification de lecture avec la pochette, et relaie les boutons de l'écran
 * verrouillé, du casque et de la voiture vers la page Onde.
 */
public class PlayerService extends Service {
    static final String CHANNEL = "onde_playback";
    static final int NOTIF_ID = 1;
    static final String ACT_PREV = "fr.onde.app.PREV";
    static final String ACT_TOGGLE = "fr.onde.app.TOGGLE";
    static final String ACT_NEXT = "fr.onde.app.NEXT";
    static final long IDLE_STOP_MS = 5 * 60 * 1000L;  // en pause depuis 5 min : le service s'arrête

    static volatile PlayerService instance;
    private static volatile Object[] pending;  // état reçu avant que le service n'ait démarré

    private final Handler main = new Handler(Looper.getMainLooper());
    private NotificationManager nm;
    private MediaSession session;
    private PowerManager.WakeLock wake;
    private WifiManager.WifiLock wifi;

    private boolean playing;
    private String title = "", artist = "", album = "", artUrl = "";
    private long posMs, durMs;
    private Bitmap art;
    private String artLoaded = "";
    private final Runnable idleStop = new Runnable() {
        @Override public void run() { shutdown(); }
    };

    // ------------------------------------------------------------------ démarrage
    /** Transmet l'état de lecture ; démarre le service à la première lecture (activité visible). */
    static void push(Context ctx, boolean playing, String title, String artist, String album, String art,
                     long pos, long dur) {
        PlayerService s = instance;
        if (s != null) {
            s.update(playing, title, artist, album, art, pos, dur);
            return;
        }
        if (!playing) return;  // rien à afficher tant que rien ne joue
        pending = new Object[]{playing, title, artist, album, art, pos, dur};
        try {
            ctx.startService(new Intent(ctx, PlayerService.class));
        } catch (Exception ignored) {
            // appli en arrière-plan et service arrêté : impossible de le relancer sans action de l'utilisateur
        }
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        Net.init(this);
        instance = this;
        Poller.ensure(this);  // la lecture garde l'appli éveillée : les notifications continuent d'arriver
        nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        createChannel();
        session = new MediaSession(this, "Onde");
        session.setFlags(MediaSession.FLAG_HANDLES_MEDIA_BUTTONS | MediaSession.FLAG_HANDLES_TRANSPORT_CONTROLS);
        session.setCallback(new MediaSession.Callback() {
            @Override public void onPlay() { MainActivity.control("play()"); }
            @Override public void onPause() { MainActivity.control("pause()"); }
            @Override public void onStop() { MainActivity.control("pause()"); }
            @Override public void onSkipToNext() { MainActivity.control("next()"); }
            @Override public void onSkipToPrevious() { MainActivity.control("prev()"); }
            @Override public void onSeekTo(long pos) { MainActivity.control("seek(" + (pos / 1000.0) + ")"); }
        });
        session.setSessionActivity(openAppIntent());
        session.setActive(true);
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "onde:playback");
            wake.setReferenceCounted(false);
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            wifi = wm.createWifiLock(WifiManager.WIFI_MODE_FULL, "onde:playback");
            wifi.setReferenceCounted(false);
        } catch (Exception ignored) {}
        publish();
        Object[] p = pending;
        pending = null;
        if (p != null) {
            update((Boolean) p[0], (String) p[1], (String) p[2], (String) p[3], (String) p[4], (Long) p[5], (Long) p[6]);
        }
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        String a = intent == null ? null : intent.getAction();
        if (ACT_PREV.equals(a)) MainActivity.control("prev()");
        else if (ACT_NEXT.equals(a)) MainActivity.control("next()");
        else if (ACT_TOGGLE.equals(a)) MainActivity.control("toggle()");
        publish();
        return START_NOT_STICKY;
    }

    @Override public void onTaskRemoved(Intent rootIntent) {  // appli fermée depuis les applications récentes
        shutdown();
    }

    @Override public void onDestroy() {
        instance = null;
        main.removeCallbacksAndMessages(null);
        try { if (wake != null && wake.isHeld()) wake.release(); } catch (Exception ignored) {}
        try { if (wifi != null && wifi.isHeld()) wifi.release(); } catch (Exception ignored) {}
        if (session != null) { session.setActive(false); session.release(); }
        super.onDestroy();
    }

    private void shutdown() {
        stopForeground(true);
        stopSelf();
    }

    // ------------------------------------------------------------------ état envoyé par la page
    /** Appelé par la page Onde (via MainActivity) à chaque changement de titre ou de lecture. */
    void update(final boolean nowPlaying, final String t, final String ar, final String al, final String art,
                final long pos, final long dur) {
        main.post(new Runnable() {
            @Override public void run() {
                playing = nowPlaying;
                title = t == null ? "" : t;
                artist = ar == null ? "" : ar;
                album = al == null ? "" : al;
                artUrl = art == null ? "" : art;
                posMs = pos;
                durMs = dur;
                main.removeCallbacks(idleStop);
                if (!playing) main.postDelayed(idleStop, IDLE_STOP_MS);
                locks(playing);
                if (!artUrl.isEmpty() && !artUrl.equals(artLoaded)) loadArt(artUrl);
                publish();
            }
        });
    }

    private void locks(boolean on) {
        try {
            if (on) {
                if (wake != null) wake.acquire(4 * 60 * 60 * 1000L);  // renouvelé à chaque mise à jour
                if (wifi != null && !wifi.isHeld()) wifi.acquire();
            } else {
                if (wake != null && wake.isHeld()) wake.release();
                if (wifi != null && wifi.isHeld()) wifi.release();
            }
        } catch (Exception ignored) {}
    }

    private void loadArt(final String url) {
        artLoaded = url;
        new Thread(new Runnable() {
            @Override public void run() {
                Bitmap bmp = null;
                HttpURLConnection c = null;
                try {
                    c = Net.open(url, 4000, 8000);
                    String cookie = CookieManager.getInstance().getCookie(url);
                    if (cookie != null) c.setRequestProperty("Cookie", cookie);
                    if (c.getResponseCode() == 200) {
                        InputStream in = c.getInputStream();
                        BitmapFactory.Options o = new BitmapFactory.Options();
                        o.inPreferredConfig = Bitmap.Config.RGB_565;
                        bmp = BitmapFactory.decodeStream(in, null, o);
                        in.close();
                    }
                } catch (Exception ignored) {
                } finally {
                    if (c != null) c.disconnect();
                }
                final Bitmap result = bmp;
                main.post(new Runnable() {
                    @Override public void run() {
                        if (!url.equals(artLoaded)) return;  // un autre titre est arrivé entre-temps
                        art = result;
                        publish();
                    }
                });
            }
        }).start();
    }

    // ------------------------------------------------------------------ notification + écran verrouillé
    private PendingIntent openAppIntent() {
        Intent i = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        return PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private PendingIntent action(String act, int code) {
        Intent i = new Intent(this, PlayerService.class).setAction(act);
        return PendingIntent.getService(this, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < 26) return;
        try {  // l'API 26 n'est pas dans la bibliothèque de compilation : appel par réflexion
            Class<?> cc = Class.forName("android.app.NotificationChannel");
            Object ch = cc.getConstructor(String.class, CharSequence.class, int.class)
                    .newInstance(CHANNEL, "Lecture", 2);  // IMPORTANCE_LOW : silencieux
            cc.getMethod("setShowBadge", boolean.class).invoke(ch, false);
            NotificationManager.class.getMethod("createNotificationChannel", cc).invoke(nm, ch);
        } catch (Exception ignored) {}
    }

    private void publish() {
        boolean has = !title.isEmpty();
        // écran verrouillé, casque, voiture
        MediaMetadata.Builder md = new MediaMetadata.Builder()
                .putString(MediaMetadata.METADATA_KEY_TITLE, has ? title : "Onde")
                .putString(MediaMetadata.METADATA_KEY_ARTIST, artist)
                .putString(MediaMetadata.METADATA_KEY_ALBUM, album);
        if (durMs > 0) md.putLong(MediaMetadata.METADATA_KEY_DURATION, durMs);
        if (art != null) md.putBitmap(MediaMetadata.METADATA_KEY_ALBUM_ART, art);
        session.setMetadata(md.build());
        long actions = PlaybackState.ACTION_PLAY | PlaybackState.ACTION_PAUSE | PlaybackState.ACTION_PLAY_PAUSE
                | PlaybackState.ACTION_SKIP_TO_NEXT | PlaybackState.ACTION_SKIP_TO_PREVIOUS | PlaybackState.ACTION_SEEK_TO
                | PlaybackState.ACTION_STOP;
        int state = !has ? PlaybackState.STATE_NONE : (playing ? PlaybackState.STATE_PLAYING : PlaybackState.STATE_PAUSED);
        session.setPlaybackState(new PlaybackState.Builder().setActions(actions)
                .setState(state, posMs, playing ? 1f : 0f, SystemClock.elapsedRealtime()).build());

        Notification.Builder b = new Notification.Builder(this)
                .setSmallIcon(R.drawable.ic_stat_onde)
                .setContentTitle(has ? title : "Onde")
                .setContentText(has ? artist : "Prêt à jouer de la musique")
                .setSubText(album.isEmpty() ? null : album)
                .setContentIntent(openAppIntent())
                .setOngoing(playing)
                .setShowWhen(false)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setColor(0xFF8E70FF)
                .addAction(android.R.drawable.ic_media_previous, "Précédent", action(ACT_PREV, 1))
                .addAction(playing ? android.R.drawable.ic_media_pause : android.R.drawable.ic_media_play,
                        playing ? "Pause" : "Lecture", action(ACT_TOGGLE, 2))
                .addAction(android.R.drawable.ic_media_next, "Suivant", action(ACT_NEXT, 3))
                .setStyle(new Notification.MediaStyle()
                        .setMediaSession(session.getSessionToken())
                        .setShowActionsInCompactView(0, 1, 2));
        if (art != null) b.setLargeIcon(art);
        if (Build.VERSION.SDK_INT >= 26) {
            try { Notification.Builder.class.getMethod("setChannelId", String.class).invoke(b, CHANNEL); }
            catch (Exception ignored) {}
        }
        try {
            startForeground(NOTIF_ID, b.build());
        } catch (RuntimeException e) {  // (Android 12+ : refusé si l'appli est en arrière-plan à ce moment-là)
            nm.notify(NOTIF_ID, b.build());
        }
    }
}
