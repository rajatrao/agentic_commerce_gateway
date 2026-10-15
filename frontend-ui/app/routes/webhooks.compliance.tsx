import type { ActionFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import { forwardWebhookToBackend } from "../lib/webhooks.server";

/**
 * Mandatory GDPR compliance topics (customers/data_request, customers/redact, shop/redact).
 * Verified here, then forwarded to backend-engine, which owns all merchant data.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  const rawBody = await request.clone().text();
  const { shop, topic } = await authenticate.webhook(request);
  console.log(`Received ${topic} compliance webhook for ${shop}`);

  await forwardWebhookToBackend(rawBody, request.headers);
  return new Response();
};
