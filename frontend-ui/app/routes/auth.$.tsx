
import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { authenticate } from "../shopify.server";
import { boundary } from "@shopify/shopify-app-react-router/server";

/** Completes Shopify OAuth and session token requests. */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  await authenticate.admin(request);

  return null;
};

/** Adds the headers Shopify's embedded app boundary needs. */
export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
