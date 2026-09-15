(() => {
  "use strict";

  const fileInput = document.getElementById("file-input");
  let currentFile = null;
  let totalLabelsInFile = null;

  // Limite de corpo de requisição mais comumente documentado para funções
  // serverless do Vercel (~4.5MB) — usado só para avisar cedo; o limite real
  // pode variar por plano/config, então isso não bloqueia o envio.
  const SAFE_UPLOAD_BYTES = 4 * 1024 * 1024;

  function debounce(fn, wait) {
    let timer;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function showError(message) {
    const box = document.getElementById("error-box");
    box.textContent = message;
    box.hidden = false;
  }

  function clearError() {
    document.getElementById("error-box").hidden = true;
  }

  function showInfo(html) {
    const box = document.getElementById("info-box");
    box.innerHTML = html;
    box.hidden = false;
  }

  function clearInfo() {
    document.getElementById("info-box").hidden = true;
  }

  function showWarning(message) {
    const box = document.getElementById("size-warning");
    box.textContent = message;
    box.hidden = false;
  }

  function clearWarning() {
    document.getElementById("size-warning").hidden = true;
  }

  /** Lê o corpo da resposta uma única vez e devolve uma mensagem de erro
   * legível — incluindo o caso em que a própria plataforma (não a nossa API)
   * respondeu algo que não é JSON (ex: uma página de erro genérica do
   * Vercel por causa de um limite de tamanho de requisição). */
  async function extractErrorMessage(response) {
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (_) {
      data = null;
    }
    if (data && data.error) {
      return data.error;
    }
    const snippet = text.slice(0, 300).trim();
    return (
      `O servidor respondeu com um erro inesperado (HTTP ${response.status}), fora do ` +
      "formato esperado — provavelmente uma falha da própria plataforma (ex: limite de " +
      "tamanho da requisição no Vercel, ou timeout) em vez do nosso código." +
      (snippet ? `\n\nDetalhe: ${snippet}` : "")
    );
  }

  /** Para respostas onde esperamos JSON no sucesso (count/analyze). Lança
   * com uma mensagem legível tanto se a resposta não for OK quanto se vier
   * OK mas o corpo não for JSON válido. */
  async function parseJsonResponse(response) {
    if (!response.ok) {
      throw new Error(await extractErrorMessage(response));
    }
    const text = await response.text();
    try {
      return text ? JSON.parse(text) : {};
    } catch (_) {
      throw new Error(
        "O servidor respondeu OK, mas não devolveu JSON válido — resposta inesperada da plataforma."
      );
    }
  }

  function isMultiMode() {
    return totalLabelsInFile !== null && totalLabelsInFile > 1;
  }

  function selectedMultiMode() {
    const checked = document.querySelector('input[name="multi-mode"]:checked');
    return checked ? checked.value : null;
  }

  function buildFormData() {
    const fd = new FormData();
    fd.append("file", currentFile);

    const sheetSizeChoice = document.getElementById("sheet-size").value;
    let sheetSize = sheetSizeChoice;
    if (sheetSizeChoice === "Custom") {
      const w = document.getElementById("sheet-width").value;
      const h = document.getElementById("sheet-height").value;
      sheetSize = `${w}x${h}`;
    }
    fd.append("sheet_size", sheetSize);
    fd.append("orientation", document.getElementById("orientation").value);
    fd.append("margin_top_mm", document.getElementById("margin-top").value);
    fd.append("margin_bottom_mm", document.getElementById("margin-bottom").value);
    fd.append("margin_left_mm", document.getElementById("margin-left").value);
    fd.append("margin_right_mm", document.getElementById("margin-right").value);
    fd.append("gap_x_mm", document.getElementById("gap-x").value);
    fd.append("gap_y_mm", document.getElementById("gap-y").value);
    fd.append("dpi", document.getElementById("dpi").value);
    fd.append("fallback_width_mm", document.getElementById("fallback-width").value);
    fd.append("fallback_height_mm", document.getElementById("fallback-height").value);
    fd.append("stretch", document.getElementById("stretch").checked ? "true" : "false");

    if (isMultiMode()) {
      const mode = selectedMultiMode();
      fd.append("multi_mode", mode || "");
      if (mode === "paginate") {
        fd.append("labels_per_page", document.getElementById("labels-per-page-multi").value);
      }
    } else {
      fd.append("labels_per_page", document.getElementById("labels-per-page-single").value);
      fd.append("pages", document.getElementById("pages").value);
    }

    return fd;
  }

  function apiCall(path, formData) {
    // Em produção (Vercel), vercel.json reescreve /api/* para /api/index, e o
    // runtime entrega ao Flask o caminho de destino, não o original — por
    // isso mandamos a ação desejada também como campo do formulário, que o
    // dispatcher em /api/index usa para decidir o que fazer. Localmente
    // (flask run, sem reescrita), a rota específica já resolve sozinha e
    // esse campo extra é só ignorado.
    const action = path.replace(/^\/api\//, "");
    formData.append("action", action);
    return fetch(path, { method: "POST", body: formData });
  }

  function estimateWaitMessage() {
    if (totalLabelsInFile && totalLabelsInFile > 1) {
      const estSeconds = Math.ceil(totalLabelsInFile * 0.6);
      return `Renderizando etiquetas (o Labelary limita ~2/s) — pode levar até ~${estSeconds}s...`;
    }
    return "Renderizando etiqueta de referência...";
  }

  function setBusy(isBusy, message) {
    document.getElementById("preview-btn").disabled = isBusy;
    document.getElementById("generate-btn").disabled = isBusy;
    const status = document.getElementById("progress-status");
    status.hidden = !isBusy;
    status.textContent = message || "";
  }

  function updateMultiModeUI() {
    const mode = selectedMultiMode();
    document.getElementById("labels-per-page-multi-field").hidden = mode !== "paginate";
    if (mode === "fit-one-page") {
      document.getElementById("multi-caption").textContent =
        `Grade dimensionada para as ${totalLabelsInFile} etiquetas distintas, em 1 página.`;
    }
  }

  async function refreshAnalysis() {
    clearError();
    if (!currentFile || totalLabelsInFile === null) {
      document.getElementById("preview-btn").disabled = true;
      document.getElementById("generate-btn").disabled = true;
      clearInfo();
      return;
    }
    try {
      const response = await apiCall("/api/analyze", buildFormData());
      const data = await parseJsonResponse(response);
      document.getElementById("preview-btn").disabled = false;
      document.getElementById("generate-btn").disabled = false;

      const scalePct = (data.scale * 100).toFixed(0);
      const stretchLabel = document.getElementById("stretch").checked
        ? "esticada"
        : "proporcional, centralizada";
      let html =
        `📐 Grade calculada: <strong>${data.cols} colunas x ${data.rows} linhas</strong> = ` +
        `${data.cols * data.rows} células · Célula: ${data.cell_width_mm.toFixed(1)}x` +
        `${data.cell_height_mm.toFixed(1)}mm · Pior escala: <strong>${scalePct}%</strong> ` +
        `(${stretchLabel})`;

      if (data.low_scale_warning) {
        html +=
          `<br><br>⚠️ Ao menos uma etiqueta está sendo reduzida para <strong>${scalePct}%</strong> ` +
          "do tamanho original nessa grade. Em escalas abaixo de 50%, o código de barras pode " +
          "ficar difícil de ler fisicamente.";
      }
      showInfo(html);

      if (data.mode === "paginate") {
        document.getElementById("multi-caption").textContent =
          `📄 Serão geradas ${data.computed_pages} página(s) para cobrir as ${totalLabelsInFile} etiquetas.`;
      }
    } catch (err) {
      document.getElementById("preview-btn").disabled = true;
      document.getElementById("generate-btn").disabled = true;
      clearInfo();
      showError(err.message);
    }
  }

  const debouncedRefresh = debounce(refreshAnalysis, 400);

  async function onFileSelected() {
    const file = fileInput.files[0];
    currentFile = file || null;
    totalLabelsInFile = null;
    document.getElementById("quantity-panel").hidden = true;
    document.getElementById("single-fields").hidden = true;
    document.getElementById("multi-fields").hidden = true;
    document.getElementById("preview-container").hidden = true;
    clearInfo();
    clearError();
    clearWarning();
    document.getElementById("preview-btn").disabled = true;
    document.getElementById("generate-btn").disabled = true;

    if (!file) {
      document.getElementById("file-status").textContent = "";
      return;
    }

    if (file.size > SAFE_UPLOAD_BYTES) {
      showWarning(
        `⚠️ Esse arquivo tem ${(file.size / (1024 * 1024)).toFixed(1)}MB — funções serverless ` +
          "do Vercel costumam ter um limite de corpo de requisição em torno de 4-4.5MB. Se der " +
          "erro ao analisar/gerar, esse é o motivo mais provável; considere um arquivo menor ou " +
          "a versão Streamlit (sem esse limite)."
      );
    }

    document.getElementById("file-status").textContent = "Analisando arquivo...";
    try {
      const fd = new FormData();
      fd.append("file", file);
      const response = await apiCall("/api/count", fd);
      const data = await parseJsonResponse(response);
      totalLabelsInFile = data.total_labels_in_file;
      document.getElementById("file-status").textContent =
        totalLabelsInFile === 1
          ? `✅ Etiqueta única detectada em ${file.name}.`
          : `✅ ${totalLabelsInFile} etiquetas distintas detectadas em ${file.name}.`;

      document.getElementById("quantity-panel").hidden = false;
      if (totalLabelsInFile === 1) {
        document.getElementById("single-fields").hidden = false;
      } else {
        document.getElementById("multi-fields").hidden = false;
        document.getElementById("labels-per-page-multi").max = String(totalLabelsInFile);
        updateMultiModeUI();
      }
      await refreshAnalysis();
    } catch (err) {
      document.getElementById("file-status").textContent = "";
      showError(err.message);
    }
  }

  fileInput.addEventListener("change", onFileSelected);

  document.getElementById("sheet-size").addEventListener("change", () => {
    document.getElementById("custom-sheet-fields").hidden =
      document.getElementById("sheet-size").value !== "Custom";
    debouncedRefresh();
  });

  const watchedInputIds = [
    "sheet-width",
    "sheet-height",
    "orientation",
    "margin-top",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "gap-x",
    "gap-y",
    "dpi",
    "fallback-width",
    "fallback-height",
    "stretch",
    "labels-per-page-single",
    "pages",
    "labels-per-page-multi",
  ];
  watchedInputIds.forEach((id) => {
    const el = document.getElementById(id);
    el.addEventListener("input", debouncedRefresh);
    el.addEventListener("change", debouncedRefresh);
  });

  document.querySelectorAll('input[name="multi-mode"]').forEach((el) => {
    el.addEventListener("change", () => {
      updateMultiModeUI();
      debouncedRefresh();
    });
  });

  document.getElementById("preview-btn").addEventListener("click", async () => {
    clearError();
    setBusy(true, estimateWaitMessage());
    try {
      const response = await apiCall("/api/preview", buildFormData());
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      document.getElementById("preview-image").src = url;
      document.getElementById("preview-container").hidden = false;
    } catch (err) {
      showError(err.message);
    } finally {
      setBusy(false);
    }
  });

  document.getElementById("generate-btn").addEventListener("click", async () => {
    clearError();
    setBusy(true, estimateWaitMessage());
    try {
      const response = await apiCall("/api/generate", buildFormData());
      if (!response.ok) {
        throw new Error(await extractErrorMessage(response));
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const match = disposition.match(/filename="?([^"]+)"?/);
      const filename = match ? match[1] : "etiquetas.pdf";

      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      showError(err.message);
    } finally {
      setBusy(false);
    }
  });
})();
