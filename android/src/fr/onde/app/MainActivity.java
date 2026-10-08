package fr.onde.app;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.text.Html;
import android.text.InputType;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Onde pour Android : l'interface web d'Onde en plein écran, avec en plus ce qu'un navigateur ne sait pas faire —
 * retrouver le Pi tout seul sur le Wi-Fi, jouer en arrière-plan écran éteint (notification, écran verrouillé,
 * casque), rester sur le Wi-Fi du Pi même sans Internet, importer de la musique et télécharger.
 */
public class MainActivity extends Activity {
    private static final int BG = 0xFF08080B, ACCENT = 0xFFA48BFF, ON_ACCENT = 0xFF120D22;
    private static final int REQ_FILE = 1, REQ_STORAGE = 2, REQ_NOTIF = 3;
    /** Adresse du serveur Onde essayée toute seule au premier lancement (Tailscale : marche en Wi-Fi comme en 4G/5G). */
    static final String DEFAULT_SERVER = "100.99.41.84";
    private static final int SILENT_RETRIES = 3;  // réseau faible : on réessaie sans afficher d'erreur

    private static MainActivity current;

    private SharedPreferences prefs;
    private FrameLayout root;
    private WebView web;
    private View setupView;
    private View customView;
    private WebChromeClient.CustomViewCallback customCallback;
    private String server;                 // adresse utilisée en ce moment (maison ou à distance)
    private String pendingLink;            // page à ouvrir (depuis une notification), ex. « #/album/3 »
    private boolean errorShown;
    private boolean failoverTried;
    private boolean loadFailed;            // le chargement en cours de la page a échoué
    private int loadRetries;
    private int pickGen;
    private ValueCallback<Uri[]> fileCallback;
    private String[] pendingDownload;
    private AtomicBoolean scanCancel = new AtomicBoolean(true);

    private ConnectivityManager cm;
    private ConnectivityManager.NetworkCallback netCallback;
    private volatile Network wifiNet;

    // setup
    private TextView statusView;
    private LinearLayout resultsView;
    private EditText input, remoteInput;
    private Button scanButton, connectButton;

    // ------------------------------------------------------------------ cycle de vie
    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        current = this;
        Net.init(this);
        prefs = Prefs.sp(this);
        root = new FrameLayout(this);
        root.setBackgroundColor(BG);
        // Android 15 : l'appli s'affiche sous les barres du système — on garde la page entre les deux
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override public android.view.WindowInsets onApplyWindowInsets(View v, android.view.WindowInsets in) {
                v.setPadding(in.getSystemWindowInsetLeft(), in.getSystemWindowInsetTop(),
                        in.getSystemWindowInsetRight(), in.getSystemWindowInsetBottom());
                return in.consumeSystemWindowInsets();
            }
        });
        setContentView(root);
        askNotificationPermission();
        String home = prefs.getString(Prefs.SERVER, null), remote = prefs.getString(Prefs.REMOTE, null);
        server = prefs.getString(Prefs.ACTIVE, null);
        if (server == null) server = home != null ? home : remote;
        pendingLink = safeLink(getIntent());
        Notifier.createChannels(this);
        WatchService.apply(this);   // notifications appli fermée, si demandées
        Poller.ensure(this);        // notifications pendant que l'appli est ouverte
        watchWifi();
        if (handleJoin(getIntent())) return;  // lien d'invitation « onde://join »
        if (home == null && remote == null) {  // 1er lancement : on essaie tout de suite l'adresse par défaut
            showSetup(false);
            connect(DEFAULT_SERVER);
        } else openServer();
    }

    /** Android 13+ : les notifications (lecture, musique ajoutée…) demandent l'accord de l'utilisateur, une fois. */
    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT < 33 || !Prefs.get(this, Prefs.NOTIF, true)) return;
        if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !prefs.getBoolean("asked_notif", false)) {
            prefs.edit().putBoolean("asked_notif", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIF);
        }
    }

    /** Lien d'une notification (« #/album/3 ») : seulement des pages internes d'Onde, jamais autre chose. */
    private static String safeLink(Intent intent) {
        if (intent == null) return null;
        String l = intent.getStringExtra(Notifier.EXTRA_LINK);
        return l != null && l.matches("#/[A-Za-z0-9/_%.\\-]{0,120}") ? l : null;
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (Updater.onStatus(this, intent)) return;  // installation d'une mise à jour de l'appli
        setIntent(intent);
        if (handleJoin(intent)) return;
        String link = safeLink(intent);
        if (link == null) return;
        if (web != null && setupView == null && server != null && !errorShown) web.loadUrl(server + "/" + link);
        else pendingLink = link;
    }

    @Override protected void onResume() {
        super.onResume();
        Updater.onResume(this);
    }

    @Override protected void onPause() {
        CookieManager.getInstance().flush();  // la connexion (mot de passe) survit à la fermeture de l'appli
        super.onPause();  // (la page n'est volontairement pas mise en pause : la musique continue)
    }

    @Override protected void onDestroy() {
        scanCancel.set(true);
        try { if (netCallback != null) cm.unregisterNetworkCallback(netCallback); } catch (Exception ignored) {}
        try { if (cm != null) cm.bindProcessToNetwork(null); } catch (Exception ignored) {}
        if (current == this) current = null;
        if (isFinishing()) stopService(new Intent(this, PlayerService.class));
        if (web != null) {
            root.removeView(web);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    @Override public void onBackPressed() {
        if (customView != null) { hideCustomView(); return; }
        if (setupView != null && server != null) { openServer(); return; }  // retour de « changer de serveur »
        if (loginView != null) { moveTaskToBack(true); return; }
        if (web != null && setupView == null && web.canGoBack()) { web.goBack(); return; }
        moveTaskToBack(true);  // la musique continue ; on ne ferme pas l'appli
    }

    /** Commande de lecture venue de la notification, de l'écran verrouillé ou du casque. */
    static void control(final String call) {
        final MainActivity a = current;
        if (a == null) return;
        a.runOnUiThread(new Runnable() {
            @Override public void run() {
                if (a.web != null) a.web.evaluateJavascript("window.OndeControl&&window.OndeControl." + call, null);
            }
        });
    }

    // ------------------------------------------------------------------ Wi-Fi sans Internet
    /** Un Wi-Fi sans Internet (point d'accès du Pi) est souvent ignoré par Android au profit de la 4G :
     *  tant que le serveur est sur le réseau local, tout le trafic de l'appli est forcé sur le Wi-Fi. */
    private void watchWifi() {
        try {
            cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkRequest req = new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build();
            netCallback = new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network n) { wifiNet = n; applyBinding(); }
                @Override public void onLost(Network n) { if (n.equals(wifiNet)) { wifiNet = null; applyBinding(); } }
            };
            cm.registerNetworkCallback(req, netCallback);
        } catch (Exception ignored) {}
    }

    private void applyBinding() {
        try {
            boolean lan = server == null || Net.isLanHost(Uri.parse(server).getHost());
            Network n = wifiNet;
            cm.bindProcessToNetwork(lan ? n : null);
        } catch (Exception ignored) {}
    }

    // ------------------------------------------------------------------ page Onde
    private void openServer() {
        scanCancel.set(true);
        closeLogin();
        if (setupView != null) { root.removeView(setupView); setupView = null; }
        if (web == null) {
            try {
                web = buildWebView();
            } catch (Exception e) {
                showSetup(false);
                setStatus("Android System WebView est introuvable ou désactivé : installe-le depuis le Play Store.", true);
                return;
            }
            root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }
        web.setVisibility(View.VISIBLE);
        errorShown = false;
        final String home = prefs.getString(Prefs.SERVER, null), remote = prefs.getString(Prefs.REMOTE, null);
        if (home == null || remote == null) {  // une seule adresse : pas de choix à faire
            server = home != null ? home : remote;
            applyBinding();
            loadServer();
            return;
        }
        // deux adresses (maison + à distance) : la maison d'abord (réseau local), sinon l'adresse à distance
        final int gen = ++pickGen;
        new Thread(new Runnable() {
            @Override public void run() {
                bindWifi(true);
                final String ok = Net.pick(home, remote, 1200, 2000, new Runnable() { @Override public void run() { bindWifi(false); } });
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        if (gen != pickGen || web == null) return;
                        if (ok == null) failoverTried = true;  // rien ne répond : pas la peine de rechercher une 2e fois
                        server = ok != null ? ok : (server != null ? server : home);
                        applyBinding();
                        loadServer();
                    }
                });
            }
        }, "onde-pick").start();
    }

    private void loadServer() {
        if (web == null || server == null) return;
        prefs.edit().putString(Prefs.ACTIVE, server).apply();
        String url = server;
        if (pendingLink != null) { url = server + "/" + pendingLink; pendingLink = null; }
        web.loadUrl(url);
        Updater.maybeCheck(this, server);  // nouvelle version de l'appli sur le serveur ?
    }

    /** Force le trafic sur le Wi-Fi (adresse de la maison) ou le libère (Internet). */
    private void bindWifi(boolean wifi) {
        try { cm.bindProcessToNetwork(wifi ? wifiNet : null); } catch (Exception ignored) {}
    }

    private WebView buildWebView() {
        WebView w = new WebView(this);
        w.setBackgroundColor(BG);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);  // la lecture suivante démarre toute seule
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        s.setBuiltInZoomControls(false);
        s.setSupportZoom(false);
        s.setTextZoom(100);
        s.setUserAgentString(s.getUserAgentString() + " OndeApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        w.addJavascriptInterface(new Bridge(), "OndeApp");
        w.setOverScrollMode(View.OVER_SCROLL_NEVER);
        w.setWebViewClient(new Client());
        w.setWebChromeClient(new Chrome());
        w.setDownloadListener(new android.webkit.DownloadListener() {
            @Override public void onDownloadStart(String url, String ua, String disposition, String mime, long length) {
                download(url, ua, disposition, mime);
            }
        });
        return w;
    }

    /** Page de l'un des serveurs choisis par l'utilisateur (maison ou à distance). */
    private boolean sameServer(Uri u) {
        String h = u.getHost();
        if (h == null) return false;
        for (String s : new String[]{server, prefs.getString(Prefs.SERVER, null), prefs.getString(Prefs.REMOTE, null)}) {
            if (s != null && h.equalsIgnoreCase(Net.hostOf(s))) return true;
        }
        return false;
    }

    private boolean handleUrl(String url) {
        if (url == null) return false;
        if (url.startsWith("onde://")) {
            String cmd = url.substring(7);
            if (cmd.startsWith("join")) { handleJoinUri(Uri.parse(url)); return true; }
            if (cmd.startsWith("retry")) { errorShown = false; failoverTried = false; loadRetries = 0; openServer(); }
            else if (cmd.startsWith("scan")) showSetup(true);
            else showSetup(false);
            return true;
        }
        Uri u = Uri.parse(url);
        if (isLoginPage(u)) { showLogin(null, null); return true; }  // connexion : écran de l'appli
        String scheme = u.getScheme();
        if (scheme == null || scheme.equals("about") || scheme.equals("blob") || scheme.equals("data") || scheme.equals("javascript")) return false;
        if (scheme.equals("http") || scheme.equals("https")) {
            if (sameServer(u)) return false;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, u));  // sites externes, liens mailto: … : hors de l'appli
        } catch (ActivityNotFoundException ignored) {}
        return true;
    }

    private void showError() {
        if (errorShown || web == null) return;
        if (loadRetries < SILENT_RETRIES) {
            // réseau faible (4G qui capte mal…) : écran d'attente discret et nouvel essai, plutôt qu'une
            // page d'erreur tout de suite
            loadRetries++;
            web.loadDataWithBaseURL(null, "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'><style>"
                    + "body{margin:0;background:#08080b;color:#6e6e7a;font-family:sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center}"
                    + "</style></head><body>Connexion à Onde…</body></html>", "text/html", "UTF-8", null);
            final int attempt = loadRetries;
            web.postDelayed(new Runnable() {
                @Override public void run() { if (web != null && !errorShown && setupView == null && attempt == loadRetries) loadServer(); }
            }, 1200L * attempt);
            return;
        }
        if (!failoverTried && prefs.getString(Prefs.SERVER, null) != null && prefs.getString(Prefs.REMOTE, null) != null) {
            failoverTried = true;  // l'autre adresse (maison / à distance) répond peut-être : on retente avec elle
            loadRetries = 0;
            openServer();
            return;
        }
        errorShown = true;
        String host = TextUtils.htmlEncode(server == null ? "" : server);
        String html = "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'><style>"
                + "body{margin:0;background:#08080b;color:#f4f4f6;font-family:sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;text-align:center}"
                + "div{padding:28px;max-width:420px}h1{font-size:24px;margin:0 0 10px}p{color:#a8a8b3;line-height:1.5;margin:8px 0}"
                + "a{display:block;margin:12px 0 0;padding:14px;border-radius:14px;text-decoration:none;font-weight:700;background:#a48bff;color:#120d22}"
                + "a.o{background:transparent;color:#a48bff;border:1px solid #a48bff55}"
                + "</style></head><body><div><h1>Onde est injoignable</h1>"
                + "<p>" + host + "</p>"
                + "<p>Vérifie que le Pi est allumé et que ce téléphone est sur le même Wi-Fi (ou sur le Wi-Fi « Onde-Music » du Pi)"
                + (prefs.getString(Prefs.REMOTE, null) != null ? ", ou que Tailscale est actif si tu es à l'extérieur" : "") + ".</p>"
                + "<a href='onde://retry'>Réessayer</a><a class='o' href='onde://scan'>Chercher mon Onde</a>"
                + "<a class='o' href='onde://change'>Changer d'adresse</a></div></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "UTF-8", null);
    }

    private class Client extends WebViewClient {
        @Override public boolean shouldOverrideUrlLoading(WebView v, String url) { return handleUrl(url); }

        // API 24+ : le framework appelle cette variante (pas de @Override : absente de la bibliothèque de compilation)
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return handleUrl(r.getUrl().toString()); }

        @Override public void onReceivedError(WebView v, int code, String desc, String failingUrl) {
            if (failingUrl != null && server != null && failingUrl.startsWith(server)) { loadFailed = true; showError(); }
        }

        @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
            if (r.isForMainFrame()) { loadFailed = true; showError(); }
        }

        @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) {
            if (url != null && server != null && url.startsWith(server)) loadFailed = false;
            if (url != null && isLoginPage(Uri.parse(url))) {  // (redirection du serveur vers /login)
                v.stopLoading();
                showLogin(null, null);
            }
        }

        @Override public void onPageFinished(WebView v, String url) {
            // page d'Onde vraiment chargée : les compteurs de nouvel essai repartent de zéro
            if (url != null && server != null && url.startsWith(server) && !loadFailed) { failoverTried = false; loadRetries = 0; }
        }

        @Override public void onReceivedSslError(WebView v, SslErrorHandler h, SslError e) {
            // certificat « autorité locale » d'Onde : accepté seulement pour le serveur choisi, à une adresse privée
            // (maison, Tailscale), ET si sa clé est celle retenue à la 1re connexion (épinglage). Sinon : refus.
            Uri u = Uri.parse(e.getUrl());
            boolean ok = false;
            if (sameServer(u) && Net.isPrivateHost(u.getHost())) {
                java.security.cert.X509Certificate cert = x509(e.getCertificate());
                int port = u.getPort() == -1 ? 443 : u.getPort();
                ok = cert != null && Net.checkPin(Net.pinKey(u.getHost(), port), Net.spki(cert));
            }
            if (ok) h.proceed();
            else {
                h.cancel();
                if (sameServer(u)) Toast.makeText(MainActivity.this, "Certificat du serveur inconnu : connexion refusée. "
                        + "Si tu as réinstallé Onde, choisis à nouveau son adresse (Changer d'adresse).", Toast.LENGTH_LONG).show();
            }
        }
    }

    private class Chrome extends WebChromeClient {
        @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
            if (fileCallback != null) fileCallback.onReceiveValue(null);
            fileCallback = cb;
            try {
                startActivityForResult(p.createIntent(), REQ_FILE);
            } catch (ActivityNotFoundException e) {
                fileCallback = null;
                return false;
            }
            return true;
        }

        @Override public void onShowCustomView(View view, CustomViewCallback cb) {
            if (customView != null) { cb.onCustomViewHidden(); return; }
            customView = view;
            customCallback = cb;
            root.addView(view, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        }

        @Override public void onHideCustomView() { hideCustomView(); }
    }

    /** Le certificat (X.509) derrière l'objet SslCertificate de la WebView. */
    private static java.security.cert.X509Certificate x509(android.net.http.SslCertificate c) {
        if (c == null) return null;
        try {
            if (Build.VERSION.SDK_INT >= 29) {
                Object x = android.net.http.SslCertificate.class.getMethod("getX509Certificate").invoke(c);
                if (x instanceof java.security.cert.X509Certificate) return (java.security.cert.X509Certificate) x;
            }
            byte[] der = android.net.http.SslCertificate.saveState(c).getByteArray("x509-certificate");
            if (der == null) return null;
            return (java.security.cert.X509Certificate) java.security.cert.CertificateFactory.getInstance("X.509")
                    .generateCertificate(new java.io.ByteArrayInputStream(der));
        } catch (Exception e) {
            return null;
        }
    }

    private void hideCustomView() {
        if (customView == null) return;
        root.removeView(customView);
        customView = null;
        if (customCallback != null) customCallback.onCustomViewHidden();
        customCallback = null;
    }

    @Override protected void onActivityResult(int req, int result, Intent data) {
        if (req == REQ_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(req, result, data);
    }

    // ------------------------------------------------------------------ téléchargements
    private void download(String url, String ua, String disposition, String mime) {
        if (url.startsWith("blob:") || url.startsWith("data:")) {
            Toast.makeText(this, "Ce téléchargement n'est pas disponible dans l'appli.", Toast.LENGTH_LONG).show();
            return;
        }
        if (Build.VERSION.SDK_INT <= 28 && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE) != PackageManager.PERMISSION_GRANTED) {
            pendingDownload = new String[]{url, ua, disposition, mime};
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE);
            return;
        }
        try {
            DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) r.addRequestHeader("Cookie", cookie);  // connexion par mot de passe conservée
            if (ua != null) r.addRequestHeader("User-Agent", ua);
            if (mime != null) r.setMimeType(mime);
            String name = URLUtil.guessFileName(url, disposition, mime);
            r.setTitle(name);
            r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
            ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
            Toast.makeText(this, "Téléchargement : " + name, Toast.LENGTH_SHORT).show();
        } catch (Exception e) {
            Toast.makeText(this, "Téléchargement impossible : " + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override public void onRequestPermissionsResult(int req, String[] perms, int[] results) {
        if (req == REQ_STORAGE && pendingDownload != null) {
            String[] d = pendingDownload;
            pendingDownload = null;
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) download(d[0], d[1], d[2], d[3]);
            else Toast.makeText(this, "Autorise l'accès au stockage pour télécharger.", Toast.LENGTH_LONG).show();
        }
    }

    // ------------------------------------------------------------------ pont avec la page (window.OndeApp)
    public class Bridge {  // public : la page y accède par réflexion
        @JavascriptInterface public String server() { return server == null ? "" : server; }
        @JavascriptInterface public String version() { return "1.4"; }

        /** Se déconnecter ou changer de compte depuis la page. */
        @JavascriptInterface public void logout() {
            runOnUiThread(new Runnable() { @Override public void run() { logoutNative(); } });
        }

        @JavascriptInterface public boolean nativeAccounts() { return true; }

        @JavascriptInterface public void changeServer() {
            runOnUiThread(new Runnable() { @Override public void run() { showSetup(false); } });
        }

        /** Adresse à distance (Internet / 4G) enregistrée, ou "". */
        @JavascriptInterface public String remote() {
            String r = prefs.getString(Prefs.REMOTE, null);
            return r == null ? "" : r;
        }

        /** La page (connectée en Wi-Fi) transmet l'adresse à distance configurée sur le Pi : retenue pour plus tard. */
        @JavascriptInterface public void setRemote(String url) {
            String n = (url == null || url.trim().isEmpty()) ? null : Net.normalize(url);
            if (url != null && !url.trim().isEmpty() && n == null) return;  // adresse invalide : ignorée
            if (n != null && n.equals(prefs.getString(Prefs.SERVER, null))) return;
            Prefs.put(MainActivity.this, Prefs.REMOTE, n);
        }

        /** Réglages de l'appli : {notif, always, down, remote, canNotify}. */
        @JavascriptInterface public String prefs() {
            try {
                JSONObject o = new JSONObject();
                o.put("notif", Prefs.get(MainActivity.this, Prefs.NOTIF, true));
                o.put("always", Prefs.get(MainActivity.this, Prefs.ALWAYS, false));
                o.put("down", Prefs.get(MainActivity.this, Prefs.DOWN, true));
                o.put("remote", remote());
                o.put("canNotify", Notifier.enabled(MainActivity.this));
                return o.toString();
            } catch (Exception e) {
                return "{}";
            }
        }

        @JavascriptInterface public void setPref(String key, boolean value) {
            if (!Prefs.NOTIF.equals(key) && !Prefs.ALWAYS.equals(key) && !Prefs.DOWN.equals(key)) return;
            Prefs.set(MainActivity.this, key, value);
            WatchService.apply(MainActivity.this);
            if (value && Prefs.NOTIF.equals(key)) Poller.ensure(MainActivity.this);
        }

        /** Notification locale demandée par la page (ex. minuterie de sommeil terminée). */
        @JavascriptInterface public void notify(String title, String text) {
            Notifier.show(MainActivity.this, "page", title == null || title.isEmpty() ? "Onde" : title, text == null ? "" : text, "");
        }

        /** État de lecture, envoyé par la page : {playing, title, artist, album, art, pos, dur}. */
        @JavascriptInterface public void update(String json) {
            try {
                JSONObject o = new JSONObject(json);
                PlayerService.push(getApplicationContext(), o.optBoolean("playing"), o.optString("title"),
                        o.optString("artist"), o.optString("album"), o.optString("art"),
                        o.optLong("pos"), o.optLong("dur"));
            } catch (Exception ignored) {}
        }
    }

    // ------------------------------------------------------------------ compte (connexion, création, mot de passe oublié)
    private View loginView;
    private TextView loginStatus;
    private boolean loginBusy;

    private boolean isLoginPage(Uri u) {
        if (u == null || server == null || !sameServer(u)) return false;
        String p = u.getPath();
        return "/login".equals(p) || "/login/".equals(p);
    }

    private boolean handleJoin(Intent intent) {
        Uri d = intent == null ? null : intent.getData();
        if (d == null || !"onde".equals(d.getScheme()) || !"join".equals(d.getHost())) return false;
        handleJoinUri(d);
        return true;
    }

    /** onde://join?server=https://…&token=… : invitation reçue → serveur retenu, écran « créer mon compte ». */
    private void handleJoinUri(Uri d) {
        final String token = d.getQueryParameter("token");
        final String target = Net.normalize(d.getQueryParameter("server"));
        if (target == null || token == null || !token.matches("[A-Za-z0-9_\\-]{8,60}")) {
            Toast.makeText(this, "Lien d'invitation invalide.", Toast.LENGTH_LONG).show();
            return;
        }
        showLogin(token, "Connexion à " + target + "…");
        new Thread(new Runnable() {
            @Override public void run() {
                final String ok = Net.probe(target, 4000, 6000);
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        if (ok == null) { setLoginStatus("Serveur injoignable : " + target, true); return; }
                        String home = prefs.getString(Prefs.SERVER, null), remote = prefs.getString(Prefs.REMOTE, null);
                        SharedPreferences.Editor ed = prefs.edit().putString(Prefs.ACTIVE, ok);
                        if (home == null) ed.putString(Prefs.SERVER, ok);
                        else if (!ok.equals(home) && !ok.equals(remote)) ed.putString(Prefs.REMOTE, ok);
                        ed.apply();
                        server = ok;
                        applyBinding();
                        showLogin(token, null);
                    }
                });
            }
        }).start();
    }

    private EditText field(String hint, int type) {
        EditText e = new EditText(this);
        e.setHint(hint);
        e.setHintTextColor(0xFF6E6E7A);
        e.setTextColor(Color.WHITE);
        e.setSingleLine(true);
        e.setInputType(type);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(6);
        e.setLayoutParams(lp);
        return e;
    }

    private void setLoginStatus(String s, boolean error) {
        if (loginStatus == null) return;
        loginStatus.setText(s == null ? "" : s);
        loginStatus.setTextColor(error ? 0xFFFF8A8A : 0xFFA8A8B3);
    }

    /** L'écran de compte. `invite` non nul : on commence par « créer mon compte » avec cette invitation. */
    private void showLogin(final String invite, String status) {
        scanCancel.set(true);
        if (web != null) web.setVisibility(View.GONE);
        if (setupView != null) { root.removeView(setupView); setupView = null; }
        if (loginView != null) root.removeView(loginView);

        ScrollView sv = new ScrollView(this);
        sv.setFillViewport(true);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        col.setPadding(dp(24), dp(40), dp(24), dp(32));
        sv.addView(col, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher);
        col.addView(logo, new LinearLayout.LayoutParams(dp(72), dp(72)));
        final TextView title = text(invite != null ? "Créer mon compte" : "Connexion", 28, Color.WHITE, true);
        title.setPadding(0, dp(10), 0, 0);
        col.addView(title);
        TextView where = text(server == null ? "" : server.replace("https://", "").replace("http://", ""), 13, 0xFF6E6E7A, false);
        where.setPadding(0, dp(2), 0, dp(18));
        col.addView(where);

        final EditText name = field("Pseudo", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PERSON_NAME);
        name.setText(prefs.getString("last_name", ""));
        final EditText pwd = field("Mot de passe", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        col.addView(name);
        col.addView(pwd);

        // champs de création de compte (invitation)
        final LinearLayout signup = new LinearLayout(this);
        signup.setOrientation(LinearLayout.VERTICAL);
        final EditText pwd2 = field("Confirme le mot de passe", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        final EditText mail = field("Mail (facultatif, pour retrouver ton mot de passe)", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS);
        final EditText code = field("Lien ou code d'invitation", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        if (invite != null) code.setText(invite);
        signup.addView(pwd2);
        signup.addView(mail);
        signup.addView(code);
        final CheckBox accept = new CheckBox(this);
        accept.setText("J'accepte les conditions d'utilisation : usage privé, je ne partage rien à personne.");
        accept.setTextColor(0xFFC9C9D1);
        accept.setTextSize(13);
        accept.setPadding(dp(4), dp(6), 0, dp(6));
        signup.addView(accept);
        Button readTerms = button("Lire les conditions d'utilisation", false);
        readTerms.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { showTerms(); } });
        signup.addView(readTerms);
        signup.setVisibility(invite != null ? View.VISIBLE : View.GONE);
        col.addView(signup, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        final Button go = button(invite != null ? "Créer mon compte" : "Se connecter", true);
        col.addView(go);
        loginStatus = text(status == null ? "" : status, 14, 0xFFA8A8B3, false);
        loginStatus.setPadding(0, dp(10), 0, dp(4));
        col.addView(loginStatus);

        final Button toggle = button(invite != null ? "J'ai déjà un compte" : "Créer mon compte (invitation)", false);
        col.addView(toggle);
        Button forgot = button("Mot de passe oublié ?", false);
        col.addView(forgot);
        Button change = button("Changer de serveur", false);
        col.addView(change);
        TextView tip = text("Pas de compte ? Demande un lien d'invitation à l'administrateur d'Onde (Réglages → Profils → Inviter un pote).", 12, 0xFF6E6E7A, false);
        tip.setPadding(0, dp(18), 0, 0);
        col.addView(tip);

        final boolean[] creating = {invite != null};
        toggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                creating[0] = !creating[0];
                signup.setVisibility(creating[0] ? View.VISIBLE : View.GONE);
                title.setText(creating[0] ? "Créer mon compte" : "Connexion");
                go.setText(creating[0] ? "Créer mon compte" : "Se connecter");
                toggle.setText(creating[0] ? "J'ai déjà un compte" : "Créer mon compte (invitation)");
                setLoginStatus("", false);
            }
        });
        View.OnClickListener submit = new View.OnClickListener() {
            @Override public void onClick(View v) {
                String n = name.getText().toString().trim(), p = pwd.getText().toString();
                if (n.isEmpty() || p.isEmpty()) { setLoginStatus("Entre ton pseudo et ton mot de passe.", true); return; }
                if (creating[0]) {
                    if (!p.equals(pwd2.getText().toString())) { setLoginStatus("Les deux mots de passe ne correspondent pas.", true); return; }
                    if (code.getText().toString().trim().isEmpty()) { setLoginStatus("Colle le lien d'invitation reçu.", true); return; }
                    if (!accept.isChecked()) { setLoginStatus("Coche la case : il faut accepter les conditions d'utilisation.", true); return; }
                }
                hideKeyboard(pwd);
                sendAuth(creating[0], n, p, mail.getText().toString().trim(), code.getText().toString().trim());
            }
        };
        go.setOnClickListener(submit);
        pwd.setImeOptions(EditorInfo.IME_ACTION_GO);
        pwd.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override public boolean onEditorAction(TextView v, int a, android.view.KeyEvent e) { if (!creating[0]) go.performClick(); return !creating[0]; }
        });
        forgot.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { forgotPassword(); } });
        change.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { showSetup(false); } });

        loginView = sv;
        root.addView(sv, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    /** Les conditions d'utilisation (texte fourni par le serveur). */
    private void showTerms() {
        if (server == null) return;
        final String base = server;
        new Thread(new Runnable() {
            @Override public void run() {
                Net.Reply r;
                try { r = Net.call(base + "/api/terms", "GET", null, null); } catch (Exception e) { r = new Net.Reply(); }
                final Net.Reply res = r;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        String title = "Conditions d'utilisation", body = "Conditions indisponibles : vérifie la connexion au serveur.";
                        if (res.code == 200) {
                            try {
                                JSONObject j = new JSONObject(res.body);
                                title = j.optString("title", title);
                                body = j.optString("html", body);
                            } catch (Exception ignored) { }
                        }
                        TextView tv = new TextView(MainActivity.this);
                        tv.setText(Build.VERSION.SDK_INT >= 24 ? Html.fromHtml(body, Html.FROM_HTML_MODE_COMPACT) : Html.fromHtml(body));
                        tv.setTextColor(0xFFDADAE0);
                        tv.setTextSize(14);
                        tv.setPadding(dp(22), dp(8), dp(22), dp(8));
                        ScrollView sv = new ScrollView(MainActivity.this);
                        sv.addView(tv);
                        new AlertDialog.Builder(MainActivity.this, android.R.style.Theme_DeviceDefault_Dialog_Alert)
                            .setTitle(title).setView(sv).setPositiveButton("Ok", null).show();
                    }
                });
            }
        }).start();
    }

    private void hideKeyboard(View v) {
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null) imm.hideSoftInputFromWindow(v.getWindowToken(), 0);
    }

    private void sendAuth(final boolean signup, final String name, final String pwd, final String mail, final String code) {
        if (loginBusy || server == null) return;
        loginBusy = true;
        setLoginStatus(signup ? "Création du compte…" : "Connexion…", false);
        final String base = server;
        new Thread(new Runnable() {
            @Override public void run() {
                Net.Reply r;
                try {
                    JSONObject o = new JSONObject().put("name", name).put("password", pwd);
                    if (signup) o.put("email", mail).put("token", code).put("terms", true);
                    r = Net.call(base + (signup ? "/api/auth/signup" : "/api/auth/login"), "POST", o.toString(), null);
                } catch (Exception e) {
                    r = new Net.Reply();
                }
                final Net.Reply res = r;
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        loginBusy = false;
                        if (res.code == 200) {
                            CookieManager cm = CookieManager.getInstance();
                            // même session pour l'adresse de la maison et l'adresse à distance
                            for (String addr : new String[]{base, prefs.getString(Prefs.SERVER, null), prefs.getString(Prefs.REMOTE, null)}) {
                                if (addr == null) continue;
                                for (String c : res.cookies) {
                                    cm.setCookie(addr, addr.startsWith("http://") ? c.replaceAll("(?i);\\s*secure", "") : c);
                                }
                            }
                            cm.flush();
                            prefs.edit().putString("last_name", name).apply();
                            Toast.makeText(MainActivity.this, signup ? "Compte créé, bienvenue !" : "Connecté", Toast.LENGTH_SHORT).show();
                            closeLogin();
                            openServer();
                            return;
                        }
                        setLoginStatus(res.code == 0 ? "Serveur injoignable : vérifie le réseau." : detail(res.body, "Erreur " + res.code), true);
                    }
                });
            }
        }).start();
    }

    private static String detail(String body, String fallback) {
        try {
            Object d = new JSONObject(body).opt("detail");
            if (d instanceof String && !((String) d).isEmpty()) return (String) d;
        } catch (Exception ignored) {}
        return fallback;
    }

    private void forgotPassword() {
        if (server == null) return;
        final EditText mail = field("Ton pseudo (ou ton mail)", InputType.TYPE_CLASS_TEXT);
        LinearLayout box = new LinearLayout(this);
        box.setPadding(dp(20), dp(8), dp(20), 0);
        box.addView(mail);
        new android.app.AlertDialog.Builder(this)
                .setTitle("Mot de passe oublié")
                .setMessage("L'administrateur reçoit une notification et t'envoie un lien pour choisir un nouveau mot de passe (ou tu le reçois par mail si le serveur envoie des mails).")
                .setView(box)
                .setNegativeButton("Annuler", null)
                .setPositiveButton("Prévenir l'admin", new android.content.DialogInterface.OnClickListener() {
                    @Override public void onClick(android.content.DialogInterface d, int w) {
                        final String m = mail.getText().toString().trim();
                        final String base = server;
                        new Thread(new Runnable() {
                            @Override public void run() {
                                Net.Reply r;
                                try { r = Net.call(base + "/api/auth/forgot", "POST", new JSONObject().put("name", m).put("email", m).toString(), null); }
                                catch (Exception e) { r = new Net.Reply(); }
                                final Net.Reply res = r;
                                runOnUiThread(new Runnable() {
                                    @Override public void run() {
                                        if (res.code == 200) setLoginStatus("C'est noté : si ce compte existe, l'administrateur est prévenu (ou un lien t'est envoyé par mail). Il te donnera un lien pour choisir un nouveau mot de passe.", false);
                                        else setLoginStatus(detail(res.body, "Envoi impossible."), true);
                                    }
                                });
                            }
                        }).start();
                    }
                })
                .show();
    }

    private void closeLogin() {
        if (loginView != null) { root.removeView(loginView); loginView = null; }
        loginStatus = null;
    }

    /** Déconnexion (ou changement de compte) : la session est fermée sur le serveur, puis l'écran de connexion. */
    private void logoutNative() {
        final String base = server;
        final String cookie = base == null ? null : CookieManager.getInstance().getCookie(base);
        new Thread(new Runnable() {
            @Override public void run() { if (base != null) Net.call(base + "/api/auth/logout", "POST", "{}", cookie); }
        }).start();
        CookieManager.getInstance().removeAllCookies(null);
        CookieManager.getInstance().flush();
        if (web != null) web.loadUrl("about:blank");  // (la musique du compte précédent s'arrête)
        showLogin(null, "Déconnecté.");
    }

    // ------------------------------------------------------------------ choix du serveur
    private int dp(float v) { return (int) (v * getResources().getDisplayMetrics().density + 0.5f); }

    private Button button(String text, boolean filled) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(16);
        b.setTypeface(Typeface.DEFAULT_BOLD);
        b.setTextColor(filled ? ON_ACCENT : ACCENT);
        GradientDrawable g = new GradientDrawable();
        g.setCornerRadius(dp(14));
        if (filled) g.setColor(ACCENT); else { g.setColor(Color.TRANSPARENT); g.setStroke(dp(1), 0x55A48BFF); }
        b.setBackground(g);
        b.setStateListAnimator(null);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
        lp.topMargin = dp(10);
        b.setLayoutParams(lp);
        return b;
    }

    private TextView text(String s, float sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        t.setGravity(Gravity.CENTER);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private void showSetup(boolean autoScan) {
        scanCancel.set(true);
        closeLogin();
        if (web != null) web.setVisibility(View.GONE);
        if (setupView != null) root.removeView(setupView);
        bindWifi(true);  // (la recherche sur le réseau local passe par le Wi-Fi)

        ScrollView sv = new ScrollView(this);
        sv.setFillViewport(true);
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);
        col.setPadding(dp(24), dp(40), dp(24), dp(32));
        sv.addView(col, new ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        ImageView logo = new ImageView(this);
        logo.setImageResource(R.mipmap.ic_launcher);
        col.addView(logo, new LinearLayout.LayoutParams(dp(84), dp(84)));
        TextView title = text("Onde", 34, Color.WHITE, true);
        title.setPadding(0, dp(12), 0, 0);
        col.addView(title);
        TextView sub = text("Ta musique, sur ton Raspberry Pi.", 15, 0xFFA8A8B3, false);
        sub.setPadding(0, dp(4), 0, dp(24));
        col.addView(sub);
        col.addView(text("Connecte ce téléphone au même Wi-Fi que le Pi (ou à son Wi-Fi « Onde-Music » en voiture), puis :", 14, 0xFFA8A8B3, false));

        scanButton = button("Chercher mon Onde", true);
        col.addView(scanButton);
        resultsView = new LinearLayout(this);
        resultsView.setOrientation(LinearLayout.VERTICAL);
        col.addView(resultsView, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        statusView = text("", 14, 0xFFA8A8B3, false);
        statusView.setPadding(0, dp(10), 0, dp(10));
        col.addView(statusView);

        TextView or = text("ou saisis l'adresse :", 14, 0xFFA8A8B3, false);
        or.setPadding(0, dp(14), 0, dp(6));
        col.addView(or);
        input = new EditText(this);
        input.setHint(DEFAULT_SERVER + "  ou  192.168.1.50:8000");
        input.setHintTextColor(0xFF6E6E7A);
        input.setTextColor(Color.WHITE);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setImeOptions(EditorInfo.IME_ACTION_GO);
        String home = prefs.getString(Prefs.SERVER, null);
        if (home != null) input.setText(home); else if (server != null) input.setText(server); else input.setText(DEFAULT_SERVER);
        col.addView(input, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView rl = text("Adresse à distance (4G, autre Wi-Fi) — facultatif :", 14, 0xFFA8A8B3, false);
        rl.setPadding(0, dp(18), 0, dp(6));
        col.addView(rl);
        remoteInput = new EditText(this);
        remoteInput.setHint("https://onde.xxxx.ts.net");
        remoteInput.setHintTextColor(0xFF6E6E7A);
        remoteInput.setTextColor(Color.WHITE);
        remoteInput.setSingleLine(true);
        remoteInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        remoteInput.setImeOptions(EditorInfo.IME_ACTION_GO);
        String remote = prefs.getString(Prefs.REMOTE, null);
        if (remote != null) remoteInput.setText(remote);
        col.addView(remoteInput, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        TextView rt = text("Laisse vide si tu n'écoutes qu'à la maison. L'appli essaie la maison d'abord, puis cette adresse. Sur le Pi : « ./start.sh remote » (elle est aussi retenue toute seule dès que tu te connectes une fois depuis la maison).", 12, 0xFF6E6E7A, false);
        rt.setPadding(0, dp(6), 0, 0);
        col.addView(rt);

        connectButton = button("Se connecter", false);
        col.addView(connectButton);
        TextView tip = text("Sur le Pi, « ./start.sh status » affiche l'adresse. En voiture : « ./start.sh hotspot » puis rejoins le Wi-Fi Onde-Music.", 12, 0xFF6E6E7A, false);
        tip.setPadding(0, dp(22), 0, 0);
        col.addView(tip);

        scanButton.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { startScan(); } });
        connectButton.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { connect(input.getText().toString()); } });
        TextView.OnEditorActionListener go = new TextView.OnEditorActionListener() {
            @Override public boolean onEditorAction(TextView v, int action, android.view.KeyEvent e) { connect(input.getText().toString()); return true; }
        };
        input.setOnEditorActionListener(go);
        remoteInput.setOnEditorActionListener(go);

        setupView = sv;
        root.addView(sv, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (autoScan) startScan();
    }

    private void setStatus(String s, boolean error) {
        if (statusView == null) return;
        statusView.setText(s);
        statusView.setTextColor(error ? 0xFFFF8A8A : 0xFFA8A8B3);
    }

    private void startScan() {
        scanCancel.set(true);
        final AtomicBoolean cancel = new AtomicBoolean(false);
        scanCancel = cancel;
        resultsView.removeAllViews();
        scanButton.setEnabled(false);
        scanButton.setAlpha(0.6f);
        setStatus("Recherche sur le réseau…", false);
        int port = Net.DEFAULT_PORT;
        try { if (server != null) port = Uri.parse(server).getPort(); } catch (Exception ignored) {}
        final int scanPort = port > 0 ? port : Net.DEFAULT_PORT;
        final String[] only = new String[1];
        Net.scan(this, server, scanPort, cancel, new Net.ScanListener() {
            @Override public void onFound(final String base) {
                if (only[0] == null) only[0] = base;
                Button b = button("Onde — " + base.replace("http://", ""), false);
                b.setOnClickListener(new View.OnClickListener() { @Override public void onClick(View v) { connect(base); } });
                resultsView.addView(b);
            }

            @Override public void onDone(int found) {
                scanButton.setEnabled(true);
                scanButton.setAlpha(1f);
                if (found == 1 && only[0] != null) { connect(only[0]); return; }  // un seul Onde : on s'y connecte
                setStatus(found == 0
                        ? "Aucun Onde trouvé. Vérifie que le Pi est allumé, sur le même Wi-Fi, ou saisis son adresse."
                        : "Choisis ton Onde :", found == 0);
            }
        });
    }

    private void connect(String typed) {
        final String base = Net.normalize(typed);
        if (base == null) { setStatus("Adresse invalide. Exemple : 192.168.1.50:8000", true); return; }
        final String remoteTyped = remoteInput == null ? "" : remoteInput.getText().toString().trim();
        final String remoteBase = remoteTyped.isEmpty() ? null : Net.normalize(remoteTyped);
        if (!remoteTyped.isEmpty() && remoteBase == null) { setStatus("Adresse à distance invalide. Exemple : https://onde.xxxx.ts.net", true); return; }
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (imm != null && input != null) imm.hideSoftInputFromWindow(input.getWindowToken(), 0);
        setStatus("Connexion à " + base + "…", false);
        connectButton.setEnabled(false);
        if (!DEFAULT_SERVER.equals(typed)) Net.forgetPin(base);  // adresse choisie à la main : sa clé est réapprise
        new Thread(new Runnable() {
            @Override public void run() {
                final String ok = Net.probe(base, 3000, 5000);
                runOnUiThread(new Runnable() {
                    @Override public void run() {
                        if (connectButton != null) connectButton.setEnabled(true);
                        if (ok == null) {
                            setStatus("Impossible de joindre " + base + ". Vérifie l'adresse, le Wi-Fi, et qu'Onde est lancé sur le Pi.", true);
                            return;
                        }
                        server = ok;
                        SharedPreferences.Editor ed = prefs.edit().putString(Prefs.SERVER, ok).putString(Prefs.ACTIVE, ok);
                        if (remoteBase == null || remoteBase.equals(ok)) ed.remove(Prefs.REMOTE); else ed.putString(Prefs.REMOTE, remoteBase);
                        ed.apply();
                        failoverTried = false;
                        loadRetries = 0;
                        openServer();
                    }
                });
            }
        }).start();
    }
}
