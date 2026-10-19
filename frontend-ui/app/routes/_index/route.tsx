import type { LoaderFunctionArgs } from "react-router";
import { redirect, Form, useLoaderData } from "react-router";

import { login } from "../../shopify.server";

import styles from "./styles.module.css";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const url = new URL(request.url);

  if (url.searchParams.get("shop")) {
    throw redirect(`/app?${url.searchParams.toString()}`);
  }

  return { showForm: Boolean(login) };
};

export default function App() {
  const { showForm } = useLoaderData<typeof loader>();

  return (
    <div className={styles.index}>
      <div className={styles.content}>
        <h1 className={styles.heading}>Agentic Commerce Gateway</h1>
        <p className={styles.text}>
          Make your catalog discoverable and purchasable by AI shopping agents.
        </p>
        {showForm && (
          <Form className={styles.form} method="post" action="/auth/login">
            <label className={styles.label}>
              <span>Shop domain</span>
              <input className={styles.input} type="text" name="shop" />
              <span>e.g: my-shop-domain.myshopify.com</span>
            </label>
            <button className={styles.button} type="submit">
              Log in
            </button>
          </Form>
        )}
        <ul className={styles.list}>
          <li>
            <strong>AI discoverability</strong>. Audit your theme&apos;s structured data and get an AI Discovery Score for every product.
          </li>
          <li>
            <strong>Agent protocols</strong>. Serve your catalog and carts over UCP, ACP and MCP from one gateway.
          </li>
          <li>
            <strong>Agentic checkout</strong>. Agents build priced carts and hand buyers to your own Shopify checkout to pay.
          </li>
        </ul>
      </div>
    </div>
  );
}
