package xyz.eastsea.forecast

import java.math.BigInteger

/** Bitcoin-alphabet base58 encoding for Solana public keys (32 bytes). */
object Base58 {
    private const val ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

    fun encode(input: ByteArray): String {
        if (input.isEmpty()) return ""
        var value = BigInteger(1, input)
        val base = BigInteger.valueOf(58)
        val out = StringBuilder()
        while (value.signum() > 0) {
            val divisionAndRemainder = value.divideAndRemainder(base)
            out.append(ALPHABET[divisionAndRemainder[1].toInt()])
            value = divisionAndRemainder[0]
        }
        for (byte in input) {
            if (byte.toInt() != 0) break
            out.append(ALPHABET[0])
        }
        return out.reverse().toString()
    }
}
