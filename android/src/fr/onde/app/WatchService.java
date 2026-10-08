package fr.onde.app;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

/**
 * Service permanent (facultatif, réglage « Notifications appli fermée ») : garde la connexion à Onde ouverte pour
 * recevoir les notifications même quand l'appli est fermée. Android impose d'afficher une notification discrète.
 */
public class WatchService extends Service {
    static final String CHANNEL = "onde_watch";
    private static final int NOTIF_ID = 2;

    /** Démarre ou arrête le service selon le réglage. */
    static void apply(Context ctx) {
        Context app = ctx.getApplicationContext();
        Intent i = new Intent(app, WatchService.class);
        try {
            if (Prefs.get(app, Prefs.ALWAYS, false) && Prefs.get(app, Prefs.NOTIF, true)) {
                if (Build.VERSION.SDK_INT >= 26) {
                    Context.class.getMethod("startForegroundService", Intent.class).invoke(app, i);
                } else {
                    app.startService(i);
                }
            } else {
                app.stopService(i);
            }
        } catch (Exception ignored) {}
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override public void onCreate() {
        super.onCreate();
        Net.init(this);
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            try {
                Class<?> cc = Class.forName("android.app.NotificationChannel");
                Object ch = cc.getConstructor(String.class, CharSequence.class, int.class)
                        .newInstance(CHANNEL, "Connexion à Onde", 1);  // IMPORTANCE_MIN : discret, sans son
                cc.getMethod("setShowBadge", boolean.class).invoke(ch, false);
                NotificationManager.class.getMethod("createNotificationChannel", cc).invoke(nm, ch);
            } catch (Exception ignored) {}
        }
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
        Notification.Builder b = new Notification.Builder(this)
                .setSmallIcon(R.drawable.ic_stat_onde)
                .setContentTitle("Onde")
                .setContentText("Connecté — notifications actives")
                .setContentIntent(PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE))
                .setOngoing(true)
                .setShowWhen(false)
                .setPriority(Notification.PRIORITY_MIN)
                .setColor(0xFF8E70FF);
        if (Build.VERSION.SDK_INT >= 26) {
            try { Notification.Builder.class.getMethod("setChannelId", String.class).invoke(b, CHANNEL); }
            catch (Exception ignored) {}
        }
        startForeground(NOTIF_ID, b.build());
        Poller.ensure(this);
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        Poller.ensure(this);
        return START_STICKY;
    }

    @Override public void onTaskRemoved(Intent rootIntent) {
        // l'appli est retirée des applications récentes : ce service continue (c'est son but)
    }
}
