/**
 * AES-256-GCM field-level encryption for sensitive database columns.
 *
 * Set FIELD_ENCRYPTION_KEY in your environment to a 64-char hex string (32 bytes):
 *   node -e "console.log(require('crypto').randomBytes(32).toString('hex'))"
 *
 * Encrypted values are stored as:  enc:v1:<base64(iv + authTag + ciphertext)>
 * Plain (legacy) values are passed through decrypt() unchanged, so existing
 * rows continue to work until they are re-written.
 */

import crypto from "crypto";

const ALGO = "aes-256-gcm";
const IV_LEN = 12;   // 96-bit IV recommended for GCM
const TAG_LEN = 16;
const PREFIX = "enc:v1:";

function getKey(): Buffer {
  const hex = process.env.FIELD_ENCRYPTION_KEY;
  if (!hex || hex.length !== 64) {
    throw new Error(
      "FIELD_ENCRYPTION_KEY must be set to a 64-character hex string (32 bytes). " +
      "Generate one with: node -e \"console.log(require('crypto').randomBytes(32).toString('hex'))\"",
    );
  }
  return Buffer.from(hex, "hex");
}

export function encrypt(plaintext: string): string {
  const key = getKey();
  const iv = crypto.randomBytes(IV_LEN);
  const cipher = crypto.createCipheriv(ALGO, key, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  // Pack: iv (12) + authTag (16) + ciphertext
  const packed = Buffer.concat([iv, tag, encrypted]);
  return PREFIX + packed.toString("base64");
}

export function decrypt(value: string): string {
  // Pass-through for legacy plaintext values (no prefix)
  if (!value.startsWith(PREFIX)) return value;

  const key = getKey();
  const packed = Buffer.from(value.slice(PREFIX.length), "base64");
  const iv = packed.subarray(0, IV_LEN);
  const tag = packed.subarray(IV_LEN, IV_LEN + TAG_LEN);
  const ciphertext = packed.subarray(IV_LEN + TAG_LEN);

  const decipher = crypto.createDecipheriv(ALGO, key, iv);
  decipher.setAuthTag(tag);
  return decipher.update(ciphertext) + decipher.final("utf8");
}

/** Encrypt only if FIELD_ENCRYPTION_KEY is configured; otherwise return plaintext. */
export function encryptIfConfigured(value: string): string {
  if (!process.env.FIELD_ENCRYPTION_KEY) return value;
  return encrypt(value);
}

/** Decrypt only if the value looks encrypted; otherwise return as-is. */
export function decryptIfEncrypted(value: string | null | undefined): string | null {
  if (!value) return value ?? null;
  if (!value.startsWith(PREFIX)) return value;
  return decrypt(value);
}
