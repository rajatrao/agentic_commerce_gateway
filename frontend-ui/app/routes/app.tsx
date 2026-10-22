import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { Outlet, useLoaderData, useRouteError } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";
import { AppProvider } from "@shopify/shopify-app-react-router/react";

import { authenticate } from "../shopify.server";

/**
 * Authenticates the embedded admin request and passes the app's API key to App
 * Bridge.
 */
export const loader = async ({ request }: LoaderFunctionArgs) => {
  await authenticate.admin(request);

  // eslint-disable-next-line no-undef
  return { apiKey: process.env.SHOPIFY_API_KEY || "" };
};

/** Layout for the embedded admin: App Bridge provider and navigation. */
export default function App() {
  const { apiKey } = useLoaderData<typeof loader>();

  return (
    <AppProvider apiKey={apiKey}>
      <s-app-nav>
        <s-link href="/app">Dashboard</s-link>
        <s-link href="/app/products">Catalog audit</s-link>
        <s-link href="/app/review">Review queue</s-link>
        <s-link href="/app/auto-approval">Auto-approval</s-link>
        <s-link href="/app/activity">Sync and jobs</s-link>
        <s-link href="/app/schemas">Category schemas</s-link>
        <s-link href="/app/mappings">Metafield mapping</s-link>
        <s-link href="/app/mapping-rules">Meta mapping rules</s-link>
        <s-link href="/app/settings">Settings</s-link>
        <s-link href="/app/agents">Agent access</s-link>
        <s-link href="/app/checkouts">Agent checkouts</s-link>
      </s-app-nav>
      <Outlet />
    </AppProvider>
  );
}

/**
 * Shopify needs React Router to catch some thrown responses, so that their
 * headers are included in the response.
 */
export function ErrorBoundary() {
  return boundary.error(useRouteError());
}

/** Adds the headers Shopify's embedded app boundary needs. */
export const headers: HeadersFunction = (headersArgs) => {
  return boundary.headers(headersArgs);
};
