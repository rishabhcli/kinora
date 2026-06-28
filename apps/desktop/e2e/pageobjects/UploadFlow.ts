import { expect, type Locator, type Page } from "@playwright/test";
import { BasePage } from "./BasePage";
import { TEXT } from "../support/selectors";

/**
 * The PDF/EPUB upload affordance inside the library. The drag-and-drop button
 * delegates to a hidden <input type="file">; Playwright drives that input
 * directly via setInputFiles. With the API mock installed, POST /api/books
 * returns an "importing" book and the upload list shows its lifecycle.
 */
export class UploadFlow extends BasePage {
  readonly fileInput: Locator;
  readonly uploadList: Locator;

  constructor(page: Page) {
    super(page);
    this.fileInput = page.locator('input[type="file"]');
    this.uploadList = page.getByRole("list", { name: /uploads in progress/i });
  }

  /** Upload an in-memory file buffer (a tiny synthetic PDF by default). */
  async uploadBuffer(
    name = "e2e-sample.pdf",
    mimeType = "application/pdf",
    buffer = makeTinyPdf(),
  ): Promise<void> {
    await this.fileInput.first().setInputFiles({ name, mimeType, buffer });
  }

  /** The upload affordance button (drag-drop zone) is visible. */
  async expectAffordanceVisible(): Promise<void> {
    await expect(this.page.getByRole("button", { name: TEXT.uploadBook })).toBeVisible();
  }

  uploadItem(titleFragment: string | RegExp): Locator {
    return this.uploadList.getByRole("listitem").filter({ hasText: titleFragment });
  }
}

/** A minimal but structurally-valid PDF (>200 bytes, the renderer's floor). */
export function makeTinyPdf(): Buffer {
  const body = `%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 58>>stream
BT /F1 24 Tf 72 700 Td (Kinora E2E sample document.) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f
trailer<</Root 1 0 R/Size 6>>
startxref
0
%%EOF
`;
  // Pad to comfortably exceed the 200-byte minimum if needed.
  return Buffer.from(body.padEnd(400, " "), "utf-8");
}
