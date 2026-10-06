import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";

const CONNECTOR_TYPES = {
  all: [
    "postgresql",
    "snowflake",
    "bigquery",
    "mysql",
    "clickhouse",
    "mongodb",
    "stripe",
    "shopify",
    "hubspot",
    "klaviyo",
    "gorgias",
    "rickandmorty",
    "custom_rest",
  ],
  rest: [
    "stripe",
    "shopify",
    "hubspot",
    "klaviyo",
    "gorgias",
    "rickandmorty",
    "custom_rest",
  ],
};

export async function GET(_req: NextRequest) {
  const session = await getServerSession(authOptions);
  if (!(session?.user as { id?: string } | undefined)?.id) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  return Response.json(CONNECTOR_TYPES);
}
