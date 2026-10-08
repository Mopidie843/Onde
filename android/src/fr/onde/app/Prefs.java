package fr.onde.app;

import android.content.Context;
import android.content.SharedPreferences;

/** Réglages de l'appli (mémorisés sur le téléphone). */
final class Prefs {
    static final String NOTIF = "notif";      // notifications d'Onde (général)
    static final String ALWAYS = "always";    // les recevoir aussi appli fermée (service permanent)
    static final String DOWN = "down";        // prévenir quand le serveur devient injoignable
    static final String SERVER = "server";    // adresse « maison » (réseau local)
    static final String REMOTE = "remote";    // adresse à distance (Internet / 4G), facultative
    static final String ACTIVE = "active";    // adresse utilisée en ce moment

    private Prefs() {}

    static SharedPreferences sp(Context c) { return c.getApplicationContext().getSharedPreferences("onde", Context.MODE_PRIVATE); }

    static boolean get(Context c, String key, boolean def) { return sp(c).getBoolean("b_" + key, def); }

    static void set(Context c, String key, boolean v) { sp(c).edit().putBoolean("b_" + key, v).apply(); }

    static String str(Context c, String key) { return sp(c).getString(key, null); }

    static void put(Context c, String key, String v) {
        SharedPreferences.Editor e = sp(c).edit();
        if (v == null) e.remove(key); else e.putString(key, v);
        e.apply();
    }
}
