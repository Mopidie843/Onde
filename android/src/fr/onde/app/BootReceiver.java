package fr.onde.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Au démarrage du téléphone : relance la connexion aux notifications si l'utilisateur l'a demandée. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context ctx, Intent intent) {
        Net.init(ctx);
        if (intent != null && Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) WatchService.apply(ctx);
    }
}
