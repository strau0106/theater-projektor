import { buildTemplate } from "@/design/template";
import { AdminClient } from "./AdminClient";

export const dynamic = "force-dynamic";

export const metadata = { title: "Die Welle – Admin" };

export default function AdminPage() {
  return <AdminClient svg={buildTemplate()} />;
}
