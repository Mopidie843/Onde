package fr.onde.app;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Notifications d'événements d'Onde (paroles synchronisées, musique ajoutée, serveur injoignable…). */
final class Notifier {
    static final String CHANNEL = "onde_events";
    static final String EXTRA_LINK = "link";

    private Notifier() {}

    static void createChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        try {  // l'API 26 n'est pas dans la bibliothèque de compilation : appels par réflexion
            Class<?> cc = Class.forName("android.app.NotificationChannel");
            Object ch = cc.getConstructor(String.class, CharSequence.class, int.class)
                    .newInstance(CHANNEL, "Nouveautés d'Onde", 3);  // IMPORTANCE_DEFAULT
            cc.getMethod("setDescription", String.class).invoke(ch, "Paroles synchronisées, musique ajoutée, téléchargements terminés…");
            NotificationManager.class.getMethod("createNotificationChannel", cc).invoke(nm, ch);
        } catch (Exception ignored) {}
    }

    /** Un identifiant stable par genre : une nouvelle notification du même genre remplace l'ancienne. */
    static int idFor(String kind) {
        return 100 + (kind == null ? 0 : Math.abs(kind.hashCode()) % 1000);
    }

    static boolean enabled(Context ctx) {
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT < 24) return true;
            // l'API 24 n'est pas dans la bibliothèque de compilation : appel par réflexion
            return (Boolean) NotificationManager.class.getMethod("areNotificationsEnabled").invoke(nm);
        } catch (Exception e) {
            return true;
        }
    }

    static void show(Context ctx, String kind, String title, String text, String link) {
        if (!Prefs.get(ctx, Prefs.NOTIF, true)) return;
        try {
            createChannels(ctx);
            NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            Intent i = new Intent(ctx, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK);
            if (link != null && !link.isEmpty()) i.putExtra(EXTRA_LINK, link);
            PendingIntent pi = PendingIntent.getActivity(ctx, idFor(kind), i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            Notification.Builder b = new Notification.Builder(ctx)
                    .setSmallIcon(R.drawable.ic_stat_onde)
                    .setContentTitle(title)
                    .setContentText(text)
                    .setStyle(new Notification.BigTextStyle().bigText(text))
                    .setContentIntent(pi)
                    .setAutoCancel(true)
                    .setColor(0xFF8E70FF)
                    .setWhen(System.currentTimeMillis());
            if (Build.VERSION.SDK_INT >= 26) {
                try { Notification.Builder.class.getMethod("setChannelId", String.class).invoke(b, CHANNEL); }
                catch (Exception ignored) {}
            }
            nm.notify(idFor(kind), b.build());
        } catch (Exception ignored) {}
    }

    static void cancel(Context ctx, String kind) {
        try {
            ((NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE)).cancel(idFor(kind));
        } catch (Exception ignored) {}
    }
}
