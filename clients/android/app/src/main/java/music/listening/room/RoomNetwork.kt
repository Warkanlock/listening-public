package music.listening.room

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import okhttp3.Call
import okhttp3.ConnectionPool
import okhttp3.Dns
import okhttp3.EventListener
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.net.InetAddress
import java.net.Proxy
import java.net.UnknownHostException
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import java.util.concurrent.TimeUnit

internal class RoomProof(private val device: String?, credential: String) {
    private val secret = MessageDigest.getInstance("SHA-256").digest(credential.toByteArray())

    fun verify(client: OkHttpClient, endpoint: HttpUrl, onCall: (Call) -> Unit) {
        val challenge = ByteArray(32).also(SecureRandom()::nextBytes).hex()
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(secret, "HmacSHA256"))
        val expected = mac.doFinal("listening-room-wifi-v1:$challenge".toByteArray()).hex()
        val request = Request.Builder().url(endpoint.newBuilder().addPathSegments("api/room/discover").build())
            .header("X-Listening-Challenge", challenge)
            .apply { if (device != null) header("X-Listening-Device", device) }.build()
        client.newCall(request).also(onCall).execute().use { response ->
            if (response.code == 401) throw PairingRequired()
            val proof = response.header("X-Listening-Proof").orEmpty()
            if (response.code != 204 || proof.isEmpty()) {
                throw IOException("Could not verify the paired room. Check the invitation and your network.")
            }
            if (!MessageDigest.isEqual(expected.toByteArray(), proof.toByteArray())) throw PairingRequired()
        }
    }

    private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
}

/** The catalog, registration and Media3 all use the route selected by the invitation. */
internal class RoomNetwork(context: Context, origin: String, private val homeWifi: Boolean = false, private val onCall: (Call) -> Unit = {}, private val proof: RoomProof? = null) : Call.Factory {
    private val endpoint = origin.toHttpUrl()
    private val manager = context.applicationContext.getSystemService(ConnectivityManager::class.java)
    private val host = endpoint.host
    private val numeric = if (':' in host || host.matches(Regex("""[0-9.]+""")))
        runCatching { InetAddress.getByName(host) }.getOrNull() else null
    val tailscale = !homeWifi && (host.trimEnd('.').endsWith(".ts.net") || numeric?.let(::tailnetAddress) == true)
    val label get() = when {
        homeWifi -> "Home Wi-Fi · private network"
        tailscale -> "Tailscale · private network"
        numeric?.isSiteLocalAddress == true || host.endsWith(".local") -> "Local network"
        else -> "Server connection"
    }
    private data class Route(val network: Network, val wifiUnderVpn: Network? = null)
    @Volatile private var bound: Route? = null
    private var routedClient: OkHttpClient? = null

    fun allowsQueueBuffering(): Boolean {
        val route = if (homeWifi) bound ?: return false else null
        return allowsQueueBuffering(route)
    }

    private fun allowsQueueBuffering(route: Route?): Boolean {
        val active = manager.activeNetwork
        val network = route?.network ?: active ?: return false
        val wifi = route?.wifiUnderVpn
        if ((!homeWifi || wifi != null) && network != active) return false
        return manager.getNetworkCapabilities(network).isUnmeteredLan() &&
            (wifi == null || manager.getNetworkCapabilities(wifi).isUnmeteredLan())
    }

    override fun newCall(request: Request): Call = newCall(request, queueBuffering = false)

    fun newQueueBufferingCall(request: Request): Call = newCall(request, queueBuffering = true)

    @Synchronized
    private fun newCall(request: Request, queueBuffering: Boolean): Call {
        val url = request.url
        if (url.host != host || url.scheme != endpoint.scheme || url.port != endpoint.port)
            throw IOException("The room returned a link to another server.")
        fun call(client: OkHttpClient, route: Route? = null): Call {
            val transfer = url.encodedPath == "/api/replica" || url.encodedPath == "/api/replica/original" || url.queryParameter("format") != null ||
                url.queryParameter("save") == "1"
            val requestClient = when {
                transfer -> client.newBuilder().readTimeout(6, TimeUnit.MINUTES).build()
                url.encodedPath == "/api/extensions/health" -> client.newBuilder().readTimeout(135, TimeUnit.SECONDS).build()
                url.encodedPath.startsWith("/api/connections/") ||
                    url.encodedPath == "/api/playlists/add-song" || url.encodedPath in setOf("/api/discovery/search", "/api/discovery/import") ->
                    client.newBuilder().readTimeout(75, TimeUnit.SECONDS).build()
                else -> client
            }
            val guardedClient = if (queueBuffering) {
                val bufferRoute = route ?: manager.activeNetwork?.let { Route(it) }
                if (bufferRoute == null || !allowsQueueBuffering(bufferRoute)) {
                    throw IOException("The room is on a metered or unavailable network.")
                }
                requestClient.newBuilder().apply {
                    if (route == null) {
                        // The normal pool can retain sockets opened on the previous default network.
                        connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS))
                        socketFactory(bufferRoute.network.socketFactory)
                        dns(object : Dns {
                            override fun lookup(hostname: String) = bufferRoute.network.getAllByName(hostname).toList()
                        })
                    }
                    eventListenerFactory { call ->
                        QueueBufferingGuard(manager, setOfNotNull(bufferRoute.network, bufferRoute.wifiUnderVpn), call) {
                            allowsQueueBuffering(bufferRoute)
                        }
                    }
                }.build()
            } else requestClient
            return guardedClient.newCall(request).also(onCall)
        }
        if (!tailscale && !homeWifi) return call(direct)
        val active = manager.activeNetwork
        val routes = if (homeWifi) {
            val wifi = manager.allNetworks.filter { it.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
                !it.hasTransport(NetworkCapabilities.TRANSPORT_VPN) }
            buildList {
                wifi.forEach { add(Route(it)) }
                // Some VPNs forbid binding past them. Only use the VPN when Android
                // reports Wi-Fi and there is one identifiable physical Wi-Fi network.
                if (active != null && wifi.size == 1 && manager.getNetworkCapabilities(active).isWifiVpn())
                    add(Route(active, wifi.single()))
            }
        } else {
            listOfNotNull(active?.takeIf { it.hasTransport(NetworkCapabilities.TRANSPORT_VPN) }?.let { Route(it) })
        }
        bound?.let { if (it in routes) return call(requireNotNull(routedClient), it) }
        var failure: IOException? = null
        for (route in routes) {
            try {
                val network = route.network
                val vpn = network.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
                val addresses = network.getAllByName(host).filter {
                    if (tailscale) tailnetAddress(it) else privateRoomAddress(it, vpn)
                }
                if (addresses.isEmpty()) throw UnknownHostException("The room needs an address on your private network.")
                val client = verifiedRoute(route, addresses)
                routedClient = client
                bound = route
                return call(client, route)
            } catch (error: IOException) {
                if (failure !is PairingRequired) failure = error
            }
        }
        throw failure ?: IOException(if (homeWifi) "Join your home Wi-Fi and try again."
            else "Turn on Tailscale on this phone, then try again.")
    }

    private fun verifiedRoute(route: Route, addresses: List<InetAddress>): OkHttpClient {
        val network = route.network
        // A new network gets a new pool so old sockets cannot cross VPN changes.
        fun client(pinned: List<InetAddress>) = direct.newBuilder().connectionPool(okhttp3.ConnectionPool())
            .proxy(Proxy.NO_PROXY).socketFactory(network.socketFactory)
            .dns(object : Dns {
                override fun lookup(hostname: String): List<InetAddress> {
                    if (hostname != host) throw UnknownHostException(hostname)
                    return pinned
                }
            }).apply {
                route.wifiUnderVpn?.let { wifi ->
                    eventListenerFactory { call -> WifiTransferGuard(manager, network, wifi, call) }
                }
            }.build()

        val challenge = proof ?: return client(addresses)
        // Verify exactly the address the credential will use. A dual-stack name
        // can have one address where the operator did not publish a port.
        var failure: IOException? = null
        for (address in addresses) {
            val candidate = client(listOf(address))
            try {
                challenge.verify(candidate, endpoint, onCall)
                return candidate
            } catch (error: IOException) {
                candidate.connectionPool.evictAll()
                if (failure !is PairingRequired) failure = error
            }
        }
        throw failure ?: UnknownHostException(host)
    }

    private fun Network.hasTransport(transport: Int) =
        manager.getNetworkCapabilities(this)?.hasTransport(transport) == true

    companion object {
        private val direct = OkHttpClient.Builder()
            .connectTimeout(6, TimeUnit.SECONDS).readTimeout(10, TimeUnit.SECONDS)
            .followRedirects(false).followSslRedirects(false).build()

        private fun tailnetAddress(address: InetAddress): Boolean {
            val bytes = address.address
            return if (bytes.size == 4)
                bytes[0].toInt() == 100 && (bytes[1].toInt() and 0xc0) == 0x40
            else bytes.take(6) == listOf(0xfd.toByte(), 0x7a, 0x11, 0x5c, 0xa1.toByte(), 0xe0.toByte())
        }

        internal fun privateRoomAddress(address: InetAddress, vpn: Boolean): Boolean {
            if (tailnetAddress(address)) return vpn
            val bytes = address.address
            return if (bytes.size == 4) address.isSiteLocalAddress
            else bytes[0].toInt() and 0xfe == 0xfc
        }
    }
}

private fun NetworkCapabilities?.isUnmeteredLan() = this != null &&
    hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED) &&
    !hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
    (hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET))

private val transferNetworks = NetworkRequest.Builder()
    .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN).build()

/** Only speculative copies use this guard. A VPN connection can survive a switch to cellular. */
private class QueueBufferingGuard(private val manager: ConnectivityManager, private val networks: Set<Network>,
                                  private val transfer: Call, private val canStart: () -> Boolean) : EventListener() {
    private var registered = false
    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onLost(network: Network) {
            if (network in networks) transfer.cancel()
        }

        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
            if (network in networks && !caps.isUnmeteredLan()) transfer.cancel()
        }
    }

    override fun callStart(call: Call) {
        try {
            manager.registerNetworkCallback(transferNetworks, callback)
            registered = true
            if (!canStart()) call.cancel()
        } catch (_: RuntimeException) {
            call.cancel()
        }
    }

    override fun callEnd(call: Call) = release()
    override fun callFailed(call: Call, ioe: IOException) = release()

    private fun release() {
        if (registered) manager.unregisterNetworkCallback(callback)
    }
}

private fun NetworkCapabilities?.isWifiVpn() = this != null &&
    hasTransport(NetworkCapabilities.TRANSPORT_VPN) && hasTransport(NetworkCapabilities.TRANSPORT_WIFI) &&
    !hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR)

/** A VPN handle can outlive its Wi-Fi connection. Watch both until the response body closes. */
private class WifiTransferGuard(private val manager: ConnectivityManager, private val vpn: Network,
                                private val wifi: Network, private val transfer: Call) : EventListener() {
    private var registered = false
    private val callback = object : ConnectivityManager.NetworkCallback() {
        override fun onLost(network: Network) {
            if (network == wifi || network == vpn) transfer.cancel()
        }
        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
            if (network == vpn && !caps.isWifiVpn()) transfer.cancel()
        }
    }

    override fun callStart(call: Call) {
        try {
            manager.registerNetworkCallback(transferNetworks, callback)
            registered = true
            if (manager.activeNetwork != vpn || !manager.getNetworkCapabilities(vpn).isWifiVpn() ||
                manager.getNetworkCapabilities(wifi)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) != true)
                call.cancel()
        } catch (_: RuntimeException) {
            call.cancel()
        }
    }
    override fun callEnd(call: Call) = release()
    override fun callFailed(call: Call, ioe: IOException) = release()
    private fun release() {
        if (registered) manager.unregisterNetworkCallback(callback)
    }
}
