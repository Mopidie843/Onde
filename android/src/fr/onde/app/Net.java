package fr.onde.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.DhcpInfo;
import android.net.wifi.WifiManager;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.MalformedURLException;
import java.net.URL;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;

/** Réseau : vérifier qu'une adresse est bien un serveur Onde, et retrouver le Pi sur le Wi-Fi. */
final class Net {
    static final int DEFAULT_PORT = 8000;
    static final String[] KNOWN_HOSTS = {"10.42.0.1", "192.168.4.1", "raspberrypi.local", "onde.local"};

    private Net() {}

    interface ScanListener {
        void onFound(String base);          // appelé sur le thread principal
        void onDone(int found);             // idem
    }

    // ------------------------------------------------------------------ adresse saisie → https://hôte:port
    private static boolean isIpv4(String host) {
        return host != null && host.matches("\\d{1,3}(\\.\\d{1,3}){3}");
    }

    /** « raspberrypi.local », « 192.168.1.50:8000 », « http://… », « onde.xxx.ts.net » → adresse complète (ou null).
     *  Sans schéma : http (port 8000) pour une adresse de la maison, https (port 443) pour un nom Internet. */
    static String normalize(String input) {
        if (input == null) return null;
        String s = input.trim();
        if (s.isEmpty()) return null;
        if (!s.contains("://")) {
            String h = s.split("[/:?#]")[0].toLowerCase();
            boolean internetName = h.contains(".") && !isIpv4(h) && !isPrivateHost(h);
            s = (internetName ? "https://" : "http://") + s;
        }
        try {
            URL u = new URL(s);
            String host = u.getHost();
            if (host == null || host.isEmpty()) return null;
            String scheme = u.getProtocol().toLowerCase();
            if (!scheme.equals("http") && !scheme.equals("https")) return null;
            int port = u.getPort();
            if (port == -1) {
                if (scheme.equals("https")) return "https://" + host;          // port 443 : pas besoin de l'écrire
                port = DEFAULT_PORT;
            }
            if (scheme.equals("https") && port == 443) return "https://" + host;
            return scheme + "://" + host + ":" + port;
        } catch (MalformedURLException e) {
            return null;
        }
    }

    /** Adresse privée où Onde n'a pas de vrai certificat : réseau local, ou Tailscale (100.64.0.0/10, chiffré). */
    static boolean isPrivateHost(String host) {
        if (host == null) return false;
        if (isLanHost(host)) return true;
        if (!host.contains(".")) return true;  // « raspberrypi », « localhost »
        String[] p = host.split("\\.");
        if (p.length == 4) {
            try {
                int a = Integer.parseInt(p[0]), b = Integer.parseInt(p[1]);
                return a == 100 && b >= 64 && b <= 127;
            } catch (NumberFormatException e) {
                return false;
            }
        }
        return false;
    }

    /** Le serveur a-t-il le même hôte et le même port que cette adresse de référence ? (cookies, redirections) */
    static String hostOf(String base) {
        try { return new URL(base).getHost(); } catch (Exception e) { return null; }
    }

    /** Réseau privé (maison, point d'accès du Pi) : là où Onde doit passer par le Wi-Fi. */
    static boolean isLanHost(String host) {
        if (host == null) return false;
        if (host.endsWith(".local")) return true;
        String[] p = host.split("\\.");
        if (p.length != 4) return false;
        try {
            int a = Integer.parseInt(p[0]), b = Integer.parseInt(p[1]);
            return a == 10 || (a == 192 && b == 168) || (a == 172 && b >= 16 && b <= 31) || (a == 169 && b == 254);
        } catch (NumberFormatException e) {
            return false;
        }
    }

    // ------------------------------------------------------------------ connexions (certificat d'Onde épinglé)
    // Le serveur Onde (maison, Tailscale) a son propre certificat, signé par son « autorité locale » : Android ne
    // la connaît pas. Plutôt que d'accepter n'importe quel certificat, l'appli retient la clé publique du serveur
    // à la première connexion (comme SSH) puis n'accepte plus que celle-là — une autre machine qui se ferait
    // passer pour ton serveur est refusée. Une adresse Internet doit, elle, avoir un vrai certificat.
    private static Context appCtx;

    static void init(Context ctx) { if (ctx != null && appCtx == null) appCtx = ctx.getApplicationContext(); }

    private static SharedPreferences pins() { return appCtx == null ? null : appCtx.getSharedPreferences("onde_pins", Context.MODE_PRIVATE); }

    static String pinKey(String host, int port) { return (host == null ? "" : host.toLowerCase()) + ":" + port; }

    static String spki(X509Certificate cert) {
        try {
            byte[] d = MessageDigest.getInstance("SHA-256").digest(cert.getPublicKey().getEncoded());
            StringBuilder sb = new StringBuilder();
            for (byte x : d) sb.append(String.format("%02x", x));
            return sb.toString();
        } catch (Exception e) {
            return null;
        }
    }

    /** true si cette clé est (ou devient, 1re connexion) celle retenue pour ce serveur. */
    static synchronized boolean checkPin(String key, String fingerprint) {
        SharedPreferences p = pins();
        if (p == null || fingerprint == null) return false;
        String known = p.getString(key, null);
        if (known == null) { p.edit().putString(key, fingerprint).apply(); return true; }
        return known.equals(fingerprint);
    }

    /** Oublie la clé retenue (l'utilisateur a choisi à nouveau ce serveur, ex. après réinstallation d'Onde). */
    static synchronized void forgetPin(String base) {
        SharedPreferences p = pins();
        if (p == null || base == null) return;
        try {
            URL u = new URL(base);
            p.edit().remove(pinKey(u.getHost(), u.getPort() == -1 ? u.getDefaultPort() : u.getPort())).apply();
        } catch (Exception ignored) {}
    }

    private static X509TrustManager systemTrust() throws Exception {
        TrustManagerFactory f = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        f.init((KeyStore) null);
        for (TrustManager t : f.getTrustManagers()) if (t instanceof X509TrustManager) return (X509TrustManager) t;
        throw new IllegalStateException("pas de vérification de certificats");
    }

    private static final class PinnedTrust implements X509TrustManager {
        private final X509TrustManager sys;
        private final String key;

        PinnedTrust(X509TrustManager sys, String key) { this.sys = sys; this.key = key; }

        @Override public void checkClientTrusted(X509Certificate[] chain, String auth) throws CertificateException {
            sys.checkClientTrusted(chain, auth);
        }

        @Override public void checkServerTrusted(X509Certificate[] chain, String auth) throws CertificateException {
            try {
                sys.checkServerTrusted(chain, auth);  // vrai certificat (ex. Tailscale) : rien de plus à faire
                return;
            } catch (CertificateException notPublic) {
                if (chain == null || chain.length == 0) throw notPublic;
                chain[0].checkValidity();
                if (!checkPin(key, spki(chain[0])))
                    throw new CertificateException("Ce serveur n'est pas celui connu de l'appli (clé différente)");
            }
        }

        @Override public X509Certificate[] getAcceptedIssuers() { return sys.getAcceptedIssuers(); }
    }

    /** Connexion vers le serveur Onde de l'utilisateur (certificat d'Onde épinglé pour une adresse privée). */
    static HttpURLConnection open(String url, int connectMs, int readMs) throws Exception {
        URL parsed = new URL(url);
        HttpURLConnection c = (HttpURLConnection) parsed.openConnection();
        if (c instanceof HttpsURLConnection && isPrivateHost(parsed.getHost())) {
            final String key = pinKey(parsed.getHost(), parsed.getPort() == -1 ? 443 : parsed.getPort());
            X509TrustManager sys = systemTrust();
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, new TrustManager[]{new PinnedTrust(sys, key)}, new SecureRandom());
            final HostnameVerifier normal = HttpsURLConnection.getDefaultHostnameVerifier();
            ((HttpsURLConnection) c).setSSLSocketFactory(ctx.getSocketFactory());
            // adresse IP / nom local absent du certificat : accepté seulement si c'est la clé épinglée
            ((HttpsURLConnection) c).setHostnameVerifier(new HostnameVerifier() {
                @Override public boolean verify(String hostname, SSLSession session) {
                    if (normal.verify(hostname, session)) return true;
                    try {
                        java.security.cert.Certificate[] peer = session.getPeerCertificates();
                        return peer.length > 0 && peer[0] instanceof X509Certificate
                                && checkPin(key, spki((X509Certificate) peer[0]));
                    } catch (Exception e) {
                        return false;
                    }
                }
            });
        }
        c.setConnectTimeout(connectMs);
        c.setReadTimeout(readMs);
        c.setInstanceFollowRedirects(false);
        c.setRequestProperty("User-Agent", "OndeApp/1.2");
        return c;
    }

    /** Réponse d'une requête de l'écran de compte : code HTTP, texte, cookies posés par le serveur. */
    static final class Reply {
        int code;
        String body = "";
        final List<String> cookies = new ArrayList<>();
    }

    /** Requête JSON vers le serveur (connexion, création de compte…), avec les cookies donnés. */
    static Reply call(String url, String method, String json, String cookie) {
        Reply r = new Reply();
        HttpURLConnection c = null;
        try {
            c = open(url, 6000, 15000);
            c.setRequestMethod(method);
            c.setRequestProperty("Accept", "application/json");
            if (cookie != null) c.setRequestProperty("Cookie", cookie);
            if (json != null) {
                byte[] data = json.getBytes("UTF-8");
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setFixedLengthStreamingMode(data.length);
                c.getOutputStream().write(data);
            }
            r.code = c.getResponseCode();
            r.body = readSome(c);
            List<String> sc = c.getHeaderFields().get("Set-Cookie");
            if (sc == null) sc = c.getHeaderFields().get("set-cookie");
            if (sc != null) r.cookies.addAll(sc);
        } catch (Exception e) {
            r.code = 0;
            r.body = e.getMessage() == null ? "" : e.getMessage();
        } finally {
            if (c != null) c.disconnect();
        }
        return r;
    }

    private static String readSome(HttpURLConnection c) {
        InputStream in = null;
        try {
            in = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in == null) return "";
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int n, total = 0;
            while ((n = in.read(buf)) > 0 && total < 8192) { out.write(buf, 0, n); total += n; }
            return out.toString("UTF-8");
        } catch (Exception e) {
            return "";
        } finally {
            if (in != null) try { in.close(); } catch (Exception ignored) {}
        }
    }

    /** Vérifie que `base` est un serveur Onde. Renvoie son adresse définitive (http devenu https si
     *  le HTTPS est activé), ou null. */
    static String probe(String base, int connectMs, int readMs) {
        return probe(base, connectMs, readMs, 0);
    }

    private static String probe(String base, int connectMs, int readMs, int depth) {
        HttpURLConnection c = null;
        try {
            c = open(base + "/api/info", connectMs, readMs);
            int code = c.getResponseCode();
            if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                String loc = c.getHeaderField("Location");
                if (depth == 0 && loc != null && loc.startsWith("https://")) {
                    URL u = new URL(loc);
                    return probe(u.getPort() == -1 || u.getPort() == 443 ? "https://" + u.getHost()
                            : "https://" + u.getHost() + ":" + u.getPort(), connectMs, readMs, 1);
                }
                return null;
            }
            String body = readSome(c);
            boolean info = code == 200 && body.contains("\"musicDir\"");
            boolean locked = code == 401 && body.contains("Connexion requise");  // mot de passe défini
            return (info || locked) ? base : null;
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** Choisit l'adresse à utiliser : la maison d'abord (réseau local, rapide), puis l'adresse à distance. */
    static String pick(String home, String remote, int connectMs, int readMs, Runnable beforeRemote) {
        if (home != null) {
            String ok = probe(home, connectMs, readMs);
            if (ok != null) return ok;
        }
        if (remote != null) {
            if (beforeRemote != null) beforeRemote.run();  // (l'appli quitte le Wi-Fi forcé de la maison)
            return probe(remote, connectMs + 1500, readMs + 2500);
        }
        return null;
    }

    // ------------------------------------------------------------------ recherche automatique
    private static String ip(int le) {
        return (le & 0xff) + "." + ((le >> 8) & 0xff) + "." + ((le >> 16) & 0xff) + "." + ((le >> 24) & 0xff);
    }

    /** Adresses à essayer : passerelle et adresses habituelles d'abord, puis tout le réseau local (/24). */
    static List<String> candidates(Context ctx, String saved) {
        Set<String> out = new LinkedHashSet<String>();
        if (saved != null) {
            try { out.add(new URL(saved).getHost()); } catch (Exception ignored) {}
        }
        String own = null;
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            DhcpInfo d = wm == null ? null : wm.getDhcpInfo();
            if (d != null) {
                if (d.gateway != 0) out.add(ip(d.gateway));
                if (d.serverAddress != 0) out.add(ip(d.serverAddress));
                if (d.ipAddress != 0) own = ip(d.ipAddress);
            }
        } catch (Exception ignored) {}
        for (String h : KNOWN_HOSTS) out.add(h);
        if (own != null) {
            String prefix = own.substring(0, own.lastIndexOf('.') + 1);
            for (int i = 1; i < 255; i++) {
                String h = prefix + i;
                if (!h.equals(own)) out.add(h);
            }
        }
        return new ArrayList<String>(out);
    }

    /** Cherche des serveurs Onde sur le réseau local ; `cancel` interrompt la recherche. */
    static void scan(final Context ctx, final String saved, final int port, final AtomicBoolean cancel,
                     final ScanListener listener) {
        final android.os.Handler main = new android.os.Handler(ctx.getMainLooper());
        final List<String> hosts = candidates(ctx, saved);
        final AtomicInteger found = new AtomicInteger();
        final AtomicInteger left = new AtomicInteger(hosts.size());
        final Set<String> seen = new LinkedHashSet<String>();
        final ExecutorService pool = Executors.newFixedThreadPool(48);
        for (final String host : hosts) {
            pool.execute(new Runnable() {
                @Override public void run() {
                    try {
                        if (cancel.get()) return;
                        final String hit = probe("http://" + host + ":" + port, 500, 1200);
                        if (hit != null) {
                            boolean first;
                            synchronized (seen) { first = seen.add(hit); }
                            if (first) {
                                found.incrementAndGet();
                                main.post(new Runnable() { @Override public void run() { if (!cancel.get()) listener.onFound(hit); } });
                            }
                        }
                    } finally {
                        if (left.decrementAndGet() == 0) {
                            main.post(new Runnable() { @Override public void run() { if (!cancel.get()) listener.onDone(found.get()); } });
                            pool.shutdown();
                        }
                    }
                }
            });
        }
        try { pool.awaitTermination(0, TimeUnit.SECONDS); } catch (InterruptedException ignored) {}
    }
}
