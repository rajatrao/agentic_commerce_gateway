import type { ActionFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import db from "../db.server";
import { forwardWebhookToBackend } from "../lib/webhooks.server";

/**
 * Deletes the shop's sessions and tells backend-engine the app was uninstalled.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  const rawBody = await request.clone().text();
  const { shop, session, topic } = await authenticate.webhook(request);

  console.log(`Received ${topic} webhook for ${shop}`);

  // Webhook requests can trigger multiple times and after an app has already been uninstalled.
  // If this webhook already ran, the session may have been deleted previously.
  if (session) {
    await db.session.deleteMany({ where: { shop } });
  }

  // backend-engine marks the merchant uninstalled and wipes its stored token.
  await forwardWebhookToBackend(rawBody, request.headers);

  return new Response();
};
