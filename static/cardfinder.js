(function () {
  const blocks = Array.from(document.querySelectorAll("[data-ebay-sales]"));
  const cache = new Map();

  function formatMoney(value) {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: "USD",
    }).format(value);
  }

  function parsePrices(text) {
    return Array.from(text.matchAll(/\$\s*([0-9,]+(?:\.[0-9]{1,2})?)/g))
      .map((match) => Number(match[1].replace(/,/g, "")))
      .filter((value) => Number.isFinite(value) && value > 0);
  }

  function average(values) {
    if (!values.length) {
      return null;
    }
    return values.reduce((total, value) => total + value, 0) / values.length;
  }

  function parseMoneyText(value) {
    const match = String(value || "").match(/\$\s*([0-9,]+(?:\.[0-9]{1,2})?)/);
    return match ? Number(match[1].replace(/,/g, "")) : null;
  }

  function compareToAverage(price, averageValue) {
    if (!price || !averageValue || averageValue <= 0) {
      return { label: "Live sold avg", status: "fair" };
    }

    const percent = ((price - averageValue) / averageValue) * 100;
    if (Math.abs(percent) < 1) {
      return { label: "Near API avg", status: "fair" };
    }

    if (percent < 0) {
      return { label: `${Math.abs(percent).toFixed(0)}% under avg`, status: "deal" };
    }

    return { label: `${percent.toFixed(0)}% over avg`, status: "high" };
  }

  function updateEbayMarketRow(block, priceText) {
    const card = block.closest(".card");
    const row = card && card.querySelector("[data-ebay-market-row]");
    if (!row) {
      return;
    }

    const price = parseMoneyText(priceText);
    const apiAverage = Number(row.dataset.apiAverage);
    const comparison = compareToAverage(price, apiAverage);
    const averageNode = row.querySelector("[data-ebay-market-average]");
    const noteNode = row.querySelector("[data-ebay-market-note]");
    if (averageNode) {
      averageNode.textContent = priceText;
    }
    if (noteNode) {
      noteNode.textContent = comparison.label;
    }
    row.classList.remove("missing", "deal", "high", "fair");
    row.classList.add(comparison.status);
  }

  function markEbayMarketUnavailable(block, message) {
    const card = block.closest(".card");
    const row = card && card.querySelector("[data-ebay-market-row]");
    if (!row) {
      return;
    }

    const averageNode = row.querySelector("[data-ebay-market-average]");
    const noteNode = row.querySelector("[data-ebay-market-note]");
    if (averageNode) {
      averageNode.textContent = "Unavailable";
    }
    if (noteNode) {
      noteNode.textContent = message || "Needs eBay key";
    }
  }

  function setupManualAverage(block) {
    const textarea = block.querySelector(".manual-sales textarea");
    const button = block.querySelector("[data-manual-average]");
    const result = block.querySelector("[data-manual-result]");
    const status = block.querySelector("[data-ebay-status]");
    const averageNode = block.querySelector("[data-ebay-average]");
    if (!textarea || !button || !result) {
      return;
    }

    button.addEventListener("click", () => {
      const prices = parsePrices(textarea.value).slice(0, 10);
      const avg = average(prices);
      if (!avg) {
        result.textContent = "No prices found.";
        return;
      }

      averageNode.textContent = formatMoney(avg);
      status.textContent = `Manual avg from ${prices.length} sold price${prices.length === 1 ? "" : "s"}`;
      result.textContent = `Used ${prices.length} price${prices.length === 1 ? "" : "s"}.`;
      updateEbayMarketRow(block, formatMoney(avg));
      block.classList.remove("unavailable");
      block.classList.add("loaded");
    });
  }

  async function loadSales(block) {
    const query = block.dataset.query;
    const status = block.querySelector("[data-ebay-status]");
    const average = block.querySelector("[data-ebay-average]");
    if (!query || block.dataset.loaded === "true") {
      return;
    }

    block.dataset.loaded = "true";
    status.textContent = "Checking sold listings...";

    try {
      let data = cache.get(query);
      if (!data) {
        const controller = new AbortController();
        const timeoutId = window.setTimeout(() => controller.abort(), 12000);
        const response = await fetch(`/api/ebay-sales?q=${encodeURIComponent(query)}`, {
          signal: controller.signal,
        });
        window.clearTimeout(timeoutId);
        data = await response.json();
        cache.set(query, data);
      }

      if (data.status === "ok") {
        average.textContent = data.average;
        status.textContent = `Avg from last ${data.count} sold listing${data.count === 1 ? "" : "s"}`;
        updateEbayMarketRow(block, data.average);
        block.classList.add("loaded");
        return;
      }

      status.textContent = data.message || "No sold prices found.";
      markEbayMarketUnavailable(block, data.message);
      block.classList.add("unavailable");
    } catch (error) {
      status.textContent = "eBay lookup unavailable.";
      markEbayMarketUnavailable(block, "Lookup failed");
      block.classList.add("unavailable");
    }
  }

  blocks.forEach(setupManualAverage);

  if ("IntersectionObserver" in window) {
    blocks.slice(0, 3).forEach(loadSales);

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            observer.unobserve(entry.target);
            loadSales(entry.target);
          }
        });
      },
      { rootMargin: "250px" }
    );

    blocks.slice(3).forEach((block) => observer.observe(block));
    return;
  }

  blocks.forEach(loadSales);
})();
