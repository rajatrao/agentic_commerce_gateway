import type { ActionFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import { forwardWebhookToBackend } from "../lib/webhooks.server";

/**
 * orders/create: backend-engine links orders placed from an agent's checkout link back to
 * the agent cart (via the `_agentic_cart_id` attribute) and stores only the order's ids.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  const rawBody = await request.clone().text();
  const { shop, topic } = await authenticate.webhook(request);
  console.log(`Received ${topic} webhook for ${shop}`);

  await forwardWebhookToBackend(rawBody, request.headers);
  return new Response();
};
