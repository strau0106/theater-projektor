import { buildTemplate } from "@/design/template";
import { ResultsClient } from "./ResultsClient";

export const dynamic = "force-dynamic";

export default function ResultsPage() {
  return <ResultsClient svg={buildTemplate()} />;
}
