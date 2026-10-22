import type { ActionFunctionArgs } from "react-router";
import { SessionNotFoundError } from "@shopify/shopify-app-react-router/server";

import { unauthenticated } from "../shopify.server";
import {
  ServiceAuthError,
  verifyBackendRequest,
} from "../lib/service-auth.server";

/**
 * POST /internal/admin-token: hands backend-engine a currently valid offline Admin API token.
 *
 * frontend-ui owns the Shopify session (and its refresh token). `unauthenticated.admin()`
 * refreshes an expiring offline token when it is close to expiry and stores the new one, so
 * backend-engine never has to rotate tokens itself. Server-to-server only; requires a
 * backend-engine service JWT whose subject matches the requested shop.
 */
export const action = async ({ request }: ActionFunctionArgs) => {
  if (request.method !== "POST") {
    return Response.json({ detail: "method not allowed" }, { status: 405 });
  }

  let claims;
  try {
    claims = await verifyBackendRequest(request, "admin-token:read");
  } catch (error) {
    if (error instanceof ServiceAuthError) {
      return Response.json({ detail: error.message }, { status: error.status });
    }
    throw error;
  }

  const body = (await request.json().catch(() => ({}))) as { shop?: string };
  if (!body.shop || body.shop !== claims.shop) {
    return Response.json(
      { detail: "shop does not match token subject" },
      { status: 403 },
    );
  }

  try {
    const { session } = await unauthenticated.admin(body.shop);
    return Response.json(
      {
        access_token: session.accessToken,
        expires_at: session.expires ? session.expires.toISOString() : null,
        scope: session.scope ?? "",
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    if (error instanceof SessionNotFoundError) {
      return Response.json(
        { detail: "no offline session for shop" },
        { status: 404 },
      );
    }
    throw error;
  }
};

/** Rejects GET: this route only accepts POST. */
export const loader = () =>
  Response.json({ detail: "method not allowed" }, { status: 405 });
