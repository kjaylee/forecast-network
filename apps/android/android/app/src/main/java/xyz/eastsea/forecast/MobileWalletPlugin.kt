package xyz.eastsea.forecast

import android.net.Uri
import android.util.Base64
import androidx.lifecycle.lifecycleScope
import com.getcapacitor.JSArray
import com.getcapacitor.JSObject
import com.getcapacitor.Plugin
import com.getcapacitor.PluginCall
import com.getcapacitor.PluginMethod
import com.getcapacitor.annotation.CapacitorPlugin
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender
import com.solana.mobilewalletadapter.clientlib.ConnectionIdentity
import com.solana.mobilewalletadapter.clientlib.MobileWalletAdapter
import com.solana.mobilewalletadapter.clientlib.Solana
import com.solana.mobilewalletadapter.clientlib.TransactionResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Message-signing-only bridge to the Mobile Wallet Adapter.
 *
 * The page never sees the wallet's keys or an auth token, only base58 addresses,
 * raw public keys and detached signatures for bytes it supplied. No transaction
 * method exists here on purpose: Forecast never moves funds.
 */
@CapacitorPlugin(name = "MobileWallet")
class MobileWalletPlugin : Plugin() {
    private lateinit var sender: ActivityResultSender
    private lateinit var adapter: MobileWalletAdapter
    private var authorized: List<Account> = emptyList()

    private data class Account(val address: String, val publicKey: ByteArray, val label: String?)

    override fun load() {
        // ActivityResultSender must be created before the activity starts; load() runs in onCreate.
        sender = ActivityResultSender(activity)
        adapter = MobileWalletAdapter(
            connectionIdentity = ConnectionIdentity(
                identityUri = Uri.parse(IDENTITY_URI),
                iconUri = Uri.parse(ICON_PATH),
                identityName = IDENTITY_NAME,
            ),
        )
        adapter.blockchain = Solana.Devnet
    }

    @PluginMethod
    fun authorize(call: PluginCall) {
        val chain = call.getString("chain") ?: "solana:devnet"
        if (chain != "solana:devnet") {
            call.reject("Only solana:devnet is supported", "ERROR_CHAIN_UNSUPPORTED")
            return
        }
        activity.lifecycleScope.launch {
            when (val result = adapter.connect(sender)) {
                is TransactionResult.Success -> {
                    authorized = result.authResult.accounts.map {
                        Account(Base58.encode(it.publicKey), it.publicKey, it.accountLabel)
                    }
                    call.resolve(JSObject().put("accounts", accountsJson()))
                }
                is TransactionResult.NoWalletFound ->
                    call.reject("No Mobile Wallet Adapter wallet is installed", "ERROR_WALLET_NOT_FOUND")
                is TransactionResult.Failure ->
                    call.reject(result.e.message ?: "Authorization failed", "ERROR_AUTHORIZATION_FAILED")
            }
        }
    }

    @PluginMethod
    fun signMessage(call: PluginCall) {
        val address = call.getString("address")
        val encoded = call.getString("message")
        val account = authorized.firstOrNull { it.address == address }
        if (account == null) {
            call.reject("Account is not authorized", "ERROR_AUTHORIZATION_FAILED")
            return
        }
        val message = try {
            Base64.decode(encoded, Base64.DEFAULT)
        } catch (e: IllegalArgumentException) {
            call.reject("Message must be base64", "ERROR_INVALID_MESSAGE")
            return
        }
        if (message.isEmpty() || message.size > MAX_MESSAGE_BYTES) {
            call.reject("Message size is out of range", "ERROR_INVALID_MESSAGE")
            return
        }
        activity.lifecycleScope.launch {
            val result = adapter.transact(sender) { _ ->
                signMessagesDetached(arrayOf(message), arrayOf(account.publicKey))
            }
            when (result) {
                is TransactionResult.Success -> {
                    val signed = result.payload.messages.firstOrNull()
                    val signature = signed?.signatures?.firstOrNull()
                    if (signed == null || signature == null || signature.size != 64 || !signed.message.contentEquals(message)) {
                        call.reject("Wallet returned an unexpected signing result", "ERROR_SIGNATURE_INVALID")
                    } else {
                        call.resolve(JSObject().put("signature", Base64.encodeToString(signature, Base64.NO_WRAP)))
                    }
                }
                is TransactionResult.NoWalletFound ->
                    call.reject("No Mobile Wallet Adapter wallet is installed", "ERROR_WALLET_NOT_FOUND")
                is TransactionResult.Failure ->
                    call.reject(result.e.message ?: "Signing failed", "ERROR_SIGNING_FAILED")
            }
        }
    }

    /**
     * Devnet RPC from the handset. Cloudflare's egress is refused by public Solana RPC, so
     * the phone fetches the blockhash and submits the wallet-signed transaction itself.
     */
    @PluginMethod
    fun latestBlockhash(call: PluginCall) {
        activity.lifecycleScope.launch(Dispatchers.IO) {
            val outcome: Result<JSObject> = runCatching {
                val result = rpc("getLatestBlockhash", JSONArray().put(JSONObject().put("commitment", "confirmed"))) as JSONObject
                val value = result.getJSONObject("value")
                JSObject().put("blockhash", value.getString("blockhash"))
                    .put("lastValidBlockHeight", value.getLong("lastValidBlockHeight"))
            }
            outcome.onSuccess { call.resolve(it) }
                .onFailure { call.reject(it.message ?: "RPC unavailable", "ERROR_RPC_UNAVAILABLE") }
        }
    }

    /**
     * The wallet co-signs a transaction the service already signed as fee payer; the phone
     * then submits it and waits briefly for confirmation. No transaction is ever built or
     * altered here: bytes in, signature out.
     */
    @PluginMethod
    fun signAndSendTransaction(call: PluginCall) {
        val encoded = call.getString("transaction")
        val transaction = try {
            Base64.decode(encoded, Base64.DEFAULT)
        } catch (e: IllegalArgumentException) {
            call.reject("Transaction must be base64", "ERROR_INVALID_TRANSACTION")
            return
        }
        if (transaction.isEmpty() || transaction.size > 1232) {
            call.reject("Transaction size is out of range", "ERROR_INVALID_TRANSACTION")
            return
        }
        activity.lifecycleScope.launch {
            val result = adapter.transact(sender) { _ ->
                signTransactions(arrayOf(transaction))
            }
            when (result) {
                is TransactionResult.Success -> {
                    val signed = result.payload.signedPayloads.firstOrNull()
                    if (signed == null) {
                        call.reject("Wallet returned no signed transaction", "ERROR_SIGNING_FAILED")
                        return@launch
                    }
                    val outcome: Result<JSObject> = withContext(Dispatchers.IO) {
                        runCatching {
                            val signature = rpc("sendTransaction", JSONArray()
                                .put(Base64.encodeToString(signed, Base64.NO_WRAP))
                                .put(JSONObject().put("encoding", "base64").put("preflightCommitment", "confirmed")
                                    .put("maxRetries", 3))).toString()
                            var slot: Long? = null
                            for (attempt in 0 until 20) {
                                val statuses = rpc("getSignatureStatuses", JSONArray().put(JSONArray().put(signature))) as JSONObject
                                val status = statuses.getJSONArray("value").optJSONObject(0)
                                if (status != null && !status.isNull("confirmationStatus") && status.getString("confirmationStatus") != "processed") {
                                    slot = status.optLong("slot"); break
                                }
                                delay(1500)
                            }
                            val response = JSObject().put("signature", signature)
                            if (slot != null) response.put("slot", slot) else response.put("slot", JSONObject.NULL)
                            response
                        }
                    }
                    outcome.onSuccess { call.resolve(it) }
                        .onFailure { call.reject(it.message ?: "Submission failed", "ERROR_RPC_UNAVAILABLE") }
                }
                is TransactionResult.NoWalletFound ->
                    call.reject("No Mobile Wallet Adapter wallet is installed", "ERROR_WALLET_NOT_FOUND")
                is TransactionResult.Failure ->
                    call.reject(result.e.message ?: "Signing failed", "ERROR_SIGNING_FAILED")
            }
        }
    }

    private fun rpc(method: String, params: JSONArray): Any {
        val connection = (URL(RPC_URL).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"; connectTimeout = 15_000; readTimeout = 20_000; doOutput = true
            setRequestProperty("Content-Type", "application/json")
        }
        val body = JSONObject().put("jsonrpc", "2.0").put("id", 1).put("method", method).put("params", params)
        connection.outputStream.use { it.write(body.toString().toByteArray()) }
        val text = connection.inputStream.bufferedReader().use { it.readText() }
        val json = JSONObject(text)
        if (json.has("error")) throw IllegalStateException("RPC " + method + ": " + json.getJSONObject("error").optString("message"))
        return json.get("result")
    }

    @PluginMethod
    fun deauthorize(call: PluginCall) {
        authorized = emptyList()
        activity.lifecycleScope.launch {
            // Best effort: the wallet may already have discarded the session.
            runCatching { adapter.disconnect(sender) }
            call.resolve(JSObject())
        }
    }

    private fun accountsJson(): JSArray {
        val array = JSArray()
        for (account in authorized) {
            array.put(
                JSObject()
                    .put("address", account.address)
                    .put("publicKey", Base64.encodeToString(account.publicKey, Base64.NO_WRAP))
                    .put("label", account.label ?: "Seeker wallet"),
            )
        }
        return array
    }

    companion object {
        const val IDENTITY_URI = "https://forecast.eastsea.xyz"
        const val ICON_PATH = "favicon.svg"
        const val IDENTITY_NAME = "Forecast"
        const val MAX_MESSAGE_BYTES = 4096
        const val RPC_URL = "https://api.devnet.solana.com"
    }
}
