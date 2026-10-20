import type { ActionFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import { forwardWebhookToBackend } from "../lib/webhooks.server";

/**
 * products/update and products/delete: backend-engine keeps its catalog snapshot current
 * between audits (identity fields, removals) and supersedes open suggestions for deleted or
 * unpublished products. Only a product summary is stored.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  const rawBody = await request.clone().text();
  const { shop, topic } = await authenticate.webhook(request);
  console.log(`Received ${topic} webhook for ${shop}`);

  await forwardWebhookToBackend(rawBody, request.headers);
  return new Response();
};
