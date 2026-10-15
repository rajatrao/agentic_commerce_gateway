/**
 * Forwards a Shopify webhook to backend-engine unchanged.
 *
 * Shopify signs the raw body, so we pass the exact bytes and the original `X-Shopify-*`
 * headers through. backend-engine re-verifies the HMAC itself and de-duplicates by
 * `X-Shopify-Webhook-Id`, so it never has to trust frontend-ui for webhook data.
 */
export async function forwardWebhookToBackend(
  rawBody: string,
  originalHeaders: Headers,
): Promise<void> {
  const base = process.env.BACKEND_INTERNAL_URL?.replace(/\/$/, "");
  if (!base) {
    throw new Error("BACKEND_INTERNAL_URL is not configured");
  }

  const headers = new Headers({ "Content-Type": "application/json" });
  originalHeaders.forEach((value, key) => {
    if (key.toLowerCase().startsWith("x-shopify-")) headers.set(key, value);
  });

  const response = await fetch(`${base}/api/v1/webhooks/shopify`, {
    method: "POST",
    headers,
    body: rawBody,
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) {
    // Throwing makes Shopify retry the delivery; backend de-duplicates retries.
    throw new Error(`backend-engine rejected webhook: ${response.status}`);
  }
}
