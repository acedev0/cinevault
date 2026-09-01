(() => {
  const sidebar = document.querySelector("[data-sidebar]");
  const scrim = document.querySelector("[data-sidebar-scrim]");
  const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
  const setSidebar = (open) => {
    if (!sidebar) return;
    sidebar.classList.toggle("open", open);
    scrim?.classList.toggle("open", open);
    document.body.classList.toggle("nav-open", open);
  };
  sidebarToggle?.addEventListener("click", () => setSidebar(!sidebar?.classList.contains("open")));
  scrim?.addEventListener("click", () => setSidebar(false));

  document.querySelectorAll("[data-auto-submit]").forEach((control) => {
    control.addEventListener("change", () => control.form?.submit());
  });

  const randomToggle = document.querySelector("[data-random-toggle]");
  randomToggle?.addEventListener("click", () => {
    const url = new URL(window.location.href);
    const enabled = randomToggle.getAttribute("aria-checked") === "true";
    if (enabled) {
      url.searchParams.set("sort", "newest");
      url.searchParams.delete("seed");
    } else {
      const seedBuffer = new Uint32Array(1);
      window.crypto?.getRandomValues?.(seedBuffer);
      const seed = (seedBuffer[0] || Date.now()) % 2147483647 || 1;
      url.searchParams.set("sort", "random");
      url.searchParams.set("seed", String(seed));
    }
    window.location.assign(`${url.pathname}?${url.searchParams.toString()}`);
  });

  const mediaGrid = document.querySelector("[data-media-grid]");
  const infiniteLoader = document.querySelector("[data-infinite-loader]");
  const infiniteToggle = document.querySelector("[data-infinite-toggle]");
  const savedInfinitePreference = window.localStorage.getItem("cinevault.infiniteScroll");
  let infiniteEnabled = savedInfinitePreference !== "false";

  const updateInfiniteToggle = () => {
    if (!infiniteToggle) return;
    infiniteToggle.setAttribute("aria-checked", String(infiniteEnabled));
    infiniteToggle.classList.toggle("active", infiniteEnabled);
  };
  updateInfiniteToggle();

  if (mediaGrid && infiniteLoader) {
    const loaderMessage = infiniteLoader.querySelector("[data-loader-message]");
    const retryButton = infiniteLoader.querySelector("[data-loader-retry]");
    let loading = false;
    let hasMore = true;
    let observer;
    let fallbackScroll;

    const stopWatching = () => {
      observer?.disconnect();
      if (fallbackScroll) window.removeEventListener("scroll", fallbackScroll);
    };
    const loadMore = async () => {
      if (loading || !hasMore) return;
      loading = true;
      mediaGrid.setAttribute("aria-busy", "true");
      infiniteLoader.classList.remove("failed");
      infiniteLoader.classList.add("loading");
      if (loaderMessage) loaderMessage.textContent = "Loading more media…";
      try {
        const url = new URL("/api/media", window.location.origin);
        const current = new URLSearchParams(window.location.search);
        ["q", "kind", "sort", "seed"].forEach((key) => {
          const value = current.get(key);
          if (value) url.searchParams.set(key, value);
        });
        if (!url.searchParams.has("seed") && infiniteLoader.dataset.seed) {
          url.searchParams.set("seed", infiniteLoader.dataset.seed);
        }
        url.searchParams.set("offset", infiniteLoader.dataset.offset || "0");
        const response = await fetch(url, { headers: { Accept: "text/html", "X-Requested-With": "CineVault" } });
        if (response.status === 401) return window.location.assign("/login");
        if (!response.ok) throw new Error("Media request failed");
        const markup = await response.text();
        if (markup.trim()) mediaGrid.insertAdjacentHTML("beforeend", markup);
        infiniteLoader.dataset.offset = response.headers.get("X-Next-Offset") || infiniteLoader.dataset.offset || "0";
        hasMore = response.headers.get("X-Has-More") === "true" && Boolean(markup.trim());
        if (!hasMore) {
          stopWatching();
          infiniteLoader.classList.remove("manual", "failed");
          infiniteLoader.classList.add("complete");
          if (loaderMessage) loaderMessage.textContent = "Everything is loaded";
        } else if (!infiniteEnabled) {
          infiniteLoader.classList.add("manual");
          if (loaderMessage) loaderMessage.textContent = "Infinite scroll is off";
        } else {
          window.setTimeout(startWatching, 0);
        }
      } catch (_error) {
        stopWatching();
        infiniteLoader.classList.add("failed");
        if (loaderMessage) loaderMessage.textContent = "Could not load more media";
        if (retryButton) retryButton.textContent = "Retry";
      } finally {
        loading = false;
        infiniteLoader.classList.remove("loading");
        mediaGrid.setAttribute("aria-busy", "false");
      }
    };
    const startWatching = () => {
      if (!hasMore || !infiniteEnabled) return;
      stopWatching();
      infiniteLoader.classList.remove("manual");
      if (loaderMessage) loaderMessage.textContent = "Scroll for more media";
      if ("IntersectionObserver" in window) {
        if (!observer) {
          observer = new IntersectionObserver(
            (entries) => entries.some((entry) => entry.isIntersecting) && loadMore(),
            { rootMargin: "700px 0px" },
          );
        }
        observer.observe(infiniteLoader);
      } else {
        fallbackScroll = () => {
          if (infiniteLoader.getBoundingClientRect().top < window.innerHeight + 700) loadMore();
        };
        window.addEventListener("scroll", fallbackScroll, { passive: true });
        fallbackScroll();
      }
    };
    const applyInfiniteMode = (enabled, persist = true) => {
      infiniteEnabled = enabled;
      if (persist) window.localStorage.setItem("cinevault.infiniteScroll", String(enabled));
      updateInfiniteToggle();
      stopWatching();
      infiniteLoader.classList.toggle("manual", !enabled);
      if (!enabled && hasMore) {
        if (loaderMessage) loaderMessage.textContent = "Infinite scroll is off";
      } else {
        startWatching();
      }
    };
    retryButton?.addEventListener("click", loadMore);
    infiniteToggle?.addEventListener("click", () => applyInfiniteMode(!infiniteEnabled));
    applyInfiniteMode(infiniteEnabled, false);
  } else {
    infiniteToggle?.addEventListener("click", () => {
      infiniteEnabled = !infiniteEnabled;
      window.localStorage.setItem("cinevault.infiniteScroll", String(infiniteEnabled));
      updateInfiniteToggle();
    });
  }

  const toast = document.querySelector("[data-toast]");
  let toastTimer;
  const showToast = (message, error = false) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("error", error);
    toast.classList.add("visible");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 4600);
  };

  const searchForm = document.querySelector("[data-bot-search]");
  document.querySelector("[data-focus-bot]")?.addEventListener("click", () => {
    const search = searchForm?.querySelector('input[type="search"]');
    search?.focus();
    search?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  const results = document.querySelector("[data-bot-results]");
  const empty = document.querySelector("[data-bot-empty]");
  const loadingPanel = document.querySelector("[data-bot-loading]");
  const loadingTitle = document.querySelector("[data-bot-loading-title]");
  const content = document.querySelector("[data-bot-content]");
  const message = document.querySelector("[data-bot-message]");
  const filters = document.querySelector("[data-bot-filters]");
  const actions = document.querySelector("[data-bot-actions]");
  const navigation = document.querySelector("[data-bot-navigation]");
  const closeResults = document.querySelector("[data-bot-close]");
  let botBusy = false;

  const setBotLoading = (active, title = "Searching Telegram") => {
    botBusy = active;
    results?.setAttribute("aria-busy", String(active));
    if (loadingTitle) loadingTitle.textContent = title;
    if (loadingPanel) loadingPanel.hidden = !active;
    if (empty) empty.hidden = active || !content?.hidden;
    if (content && active) content.hidden = true;
    searchForm?.querySelectorAll("input, button").forEach((element) => { element.disabled = active; });
  };

  const postForm = async (url, data) => {
    const response = await fetch(url, { method: "POST", body: data, headers: { Accept: "application/json" } });
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("Your session expired.");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Telegram request failed.");
    return payload;
  };

  const makeActionButton = (pageId, action) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `bot-action bot-action-${action.role}`;
    button.textContent = action.text;
    if (action.role === "status") {
      button.disabled = true;
      return button;
    }
    button.addEventListener("click", async () => {
      if (botBusy) return;
      const data = new FormData();
      data.set("csrf", searchForm?.elements.csrf?.value || "");
      data.set("page", pageId);
      data.set("action", action.id);
      setBotLoading(true, action.role === "result" ? "Requesting the file" : "Updating results");
      try {
        const payload = await postForm("/api/bot/action", data);
        if (payload.type === "media") {
          showToast(`${payload.media.title} is ready`);
          window.location.assign(payload.watch_url);
          return;
        }
        renderPage(payload.page);
      } catch (error) {
        showToast(error.message || "The bot request failed.", true);
        if (content) content.hidden = false;
      } finally {
        setBotLoading(false);
      }
    });
    return button;
  };

  const renderPage = (page) => {
    if (!page || !content || !message || !filters || !actions || !navigation) return;
    message.textContent = page.message || "Choose an option";
    filters.replaceChildren();
    actions.replaceChildren();
    navigation.replaceChildren();
    page.actions.forEach((action) => {
      const button = makeActionButton(page.id, action);
      if (action.role === "filter") filters.append(button);
      else if (action.role === "navigation" || action.role === "status") navigation.append(button);
      else actions.append(button);
    });
    filters.hidden = !filters.children.length;
    actions.hidden = !actions.children.length;
    navigation.hidden = !navigation.children.length;
    content.hidden = false;
    if (empty) empty.hidden = true;
    if (loadingPanel) loadingPanel.hidden = true;
    results?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  searchForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (botBusy) return;
    const data = new FormData(searchForm);
    setBotLoading(true);
    try {
      const payload = await postForm(searchForm.action, data);
      renderPage(payload.page);
    } catch (error) {
      showToast(error.message || "The bot search failed.", true);
      if (empty) empty.hidden = false;
    } finally {
      setBotLoading(false);
    }
  });
  closeResults?.addEventListener("click", () => {
    if (content) content.hidden = true;
    if (empty) empty.hidden = false;
  });

  const pruneForm = document.querySelector("[data-prune-form]");
  const pruneButton = document.querySelector("[data-prune-button]");
  const pollPrune = async () => {
    try {
      const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error();
      const status = await response.json();
      if (status.telegram_error) throw new Error(status.telegram_error);
      if (status.prune_running) return window.setTimeout(pollPrune, 900);
      pruneButton?.classList.remove("loading");
      showToast("Telegram cleanup complete");
      window.setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      pruneButton?.classList.remove("loading");
      showToast(error.message || "Could not check deleted files", true);
    }
  };
  pruneForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pruneButton?.classList.contains("loading")) return;
    pruneButton?.classList.add("loading");
    try {
      const payload = await postForm(pruneForm.action, new FormData(pruneForm));
      showToast(payload.status === "already_running" ? "Cleanup already running" : "Checking Telegram files…");
      window.setTimeout(pollPrune, 600);
    } catch (error) {
      pruneButton?.classList.remove("loading");
      showToast(error.message || "Cleanup failed", true);
    }
  });

  document.querySelectorAll("[data-stream-link]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      const originalLabel = button.textContent;
      try {
        const data = new FormData();
        data.set("csrf", button.dataset.csrf || "");
        data.set("days", "7");
        const payload = await postForm(`/api/media/${button.dataset.mediaId}/link`, data);
        try {
          await navigator.clipboard.writeText(payload.url);
        } catch (_clipboardError) {
          const input = document.createElement("textarea");
          input.value = payload.url;
          input.setAttribute("readonly", "");
          input.style.position = "fixed";
          input.style.opacity = "0";
          document.body.append(input);
          input.select();
          document.execCommand("copy");
          input.remove();
        }
        button.textContent = "Copied";
        showToast("VLC-ready stream link copied · expires in 7 days");
      } catch (error) {
        showToast(error.message || "Could not create the stream link", true);
      } finally {
        window.setTimeout(() => {
          button.textContent = originalLabel;
          button.disabled = false;
        }, 1800);
      }
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) {
      const search = document.querySelector('[data-bot-search] input[type="search"]');
      if (search) {
        event.preventDefault();
        search.focus();
      }
    }
    if (event.key === "Escape") setSidebar(false);
  });
})();
