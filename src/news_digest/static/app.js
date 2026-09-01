/* Cheapcoding News 前端交互：阅读模式、来源筛选、订阅、朗读、进度与渐显。 */
(function () {
  "use strict";

  var motionOK = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: no-preference)").matches;

  /* ── 阅读模式（英文 / 双语 / 中文），localStorage 记忆 ── */
  var MODE_KEY = "news-digest-mode";
  var root = document.documentElement;
  var modeButtons = document.querySelectorAll("[data-mode-btn]");

  function applyMode(mode, animate) {
    var doFade = animate && motionOK;
    if (doFade) {
      root.classList.add("mode-fade");
    }
    var commit = function () {
      root.setAttribute("data-mode", mode);
      modeButtons.forEach(function (button) {
        var pressed = button.getAttribute("data-mode-btn") === mode;
        button.setAttribute("aria-pressed", String(pressed));
      });
      if (doFade) {
        root.classList.remove("mode-fade");
      }
    };
    if (doFade) {
      window.setTimeout(commit, 160);
    } else {
      commit();
    }
    try {
      localStorage.setItem(MODE_KEY, mode);
    } catch (error) {
      /* 隐私模式下无法持久化，忽略 */
    }
  }

  var savedMode = null;
  try {
    savedMode = localStorage.getItem(MODE_KEY);
  } catch (error) {
    savedMode = null;
  }
  applyMode(savedMode === "en" || savedMode === "zh" ? savedMode : "bi", false);

  modeButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      applyMode(button.getAttribute("data-mode-btn"), true);
    });
  });

  /* ── 来源筛选（首页） ── */
  var filterBar = document.querySelector("[data-filter-bar]");
  if (filterBar) {
    filterBar.hidden = false;
    var chips = filterBar.querySelectorAll(".chip");
    var items = document.querySelectorAll("[data-source]");
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var value = chip.getAttribute("data-filter");
        chips.forEach(function (other) {
          other.setAttribute("aria-pressed", String(other === chip));
        });
        items.forEach(function (item) {
          item.hidden = value !== "*" && item.getAttribute("data-source") !== value;
        });
      });
    });
  }

  /* ── 刊期日期选择（首页与归档页） ── */
  var editionPickers = document.querySelectorAll("[data-edition-picker]");
  editionPickers.forEach(function (picker) {
    picker.addEventListener("submit", function (event) {
      event.preventDefault();
      var select = picker.querySelector("select[name='edition']");
      var target = select ? select.value : "";
      if (/^\/issues\/\d{4}-\d{2}-\d{2}\/$/.test(target)) {
        window.location.assign(target);
      }
    });
  });

  /* ── 首页订阅（仅同源端点） ── */
  var subscribeForm = document.querySelector("[data-subscribe-form]");
  if (subscribeForm && window.fetch) {
    var subscribeStatus = subscribeForm.querySelector("[data-subscribe-status]");
    var submitButton = subscribeForm.querySelector("button[type='submit']");
    subscribeForm.addEventListener("submit", function (event) {
      event.preventDefault();
      subscribeStatus.textContent = "正在提交…";
      submitButton.disabled = true;
      fetch("/subscribe/api/csrf", {
        method: "GET",
        credentials: "same-origin",
        headers: {"Accept": "application/json"}
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("暂时无法提交，请稍后重试。");
        }
        return response.json();
      }).then(function (csrf) {
        return fetch("/subscribe/api/", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Accept": "application/json",
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            email: subscribeForm.elements.email.value,
            website: subscribeForm.elements.website.value,
            csrf_token: csrf.csrf_token
          })
        });
      }).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            throw new Error(data.message || "暂时无法提交，请稍后重试。");
          }
          subscribeStatus.textContent = data.message;
          subscribeForm.elements.email.value = "";
        });
      }).catch(function (error) {
        subscribeStatus.textContent = error.message;
      }).then(function () {
        submitButton.disabled = false;
      });
    });
  }

  /* ── 阅读进度条（文章页） ── */
  var progressBar = document.querySelector("[data-read-progress]");
  var articleBody = document.querySelector("[data-article-body]");
  if (progressBar && articleBody) {
    progressBar.classList.add("is-on");
    var ticking = false;
    var updateProgress = function () {
      ticking = false;
      var doc = document.documentElement;
      var total = doc.scrollHeight - doc.clientHeight;
      var ratio = total > 0 ? Math.min(1, Math.max(0, doc.scrollTop / total)) : 0;
      progressBar.style.transform = "scaleX(" + ratio + ")";
    };
    window.addEventListener("scroll", function () {
      if (!ticking) {
        ticking = true;
        window.requestAnimationFrame(updateProgress);
      }
    }, { passive: true });
    updateProgress();
  }

  /* ── 滚动渐显（仅在允许动效时） ── */
  if (motionOK && "IntersectionObserver" in window) {
    var revealTargets = document.querySelectorAll(
      ".lead, .card, .brief-list li, .archive-entry, .pair, .learn, .story-summary"
    );
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-in");
          observer.unobserve(entry.target);
        }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    revealTargets.forEach(function (target, index) {
      target.classList.add("reveal");
      target.style.setProperty("--reveal-delay", ((index % 5) * 70) + "ms");
      observer.observe(target);
    });
  }

  /* ── 浏览器朗读（英文正文，文章页） ── */
  var ttsBar = document.querySelector("[data-tts-bar]");
  var synth = window.speechSynthesis;
  if (ttsBar && synth) {
    ttsBar.hidden = false;
    var rateSelect = ttsBar.querySelector("[data-tts-rate]");
    var playButton = ttsBar.querySelector("[data-tts-play]");
    var stopButton = ttsBar.querySelector("[data-tts-stop]");
    var paraButtons = document.querySelectorAll("[data-tts-para]");
    paraButtons.forEach(function (button) {
      button.hidden = false;
    });

    var speak = function (text, onend) {
      synth.cancel();
      var utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = "en-US";
      utterance.rate = parseFloat(rateSelect.value || "1");
      if (onend) {
        utterance.onend = onend;
      }
      synth.speak(utterance);
    };

    var clearPlaying = function () {
      paraButtons.forEach(function (button) {
        button.classList.remove("is-playing");
        button.textContent = "朗读本段";
      });
    };

    playButton.addEventListener("click", function () {
      clearPlaying();
      var paragraphs = document.querySelectorAll("[data-tts-text]");
      var text = Array.prototype.map
        .call(paragraphs, function (paragraph) {
          return paragraph.textContent;
        })
        .join("\n");
      if (text.trim()) {
        speak(text, null);
      }
    });

    stopButton.addEventListener("click", function () {
      synth.cancel();
      clearPlaying();
    });

    paraButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        var pair = button.closest(".pair");
        var paragraph = pair ? pair.querySelector("[data-tts-text]") : null;
        if (!paragraph) {
          return;
        }
        clearPlaying();
        button.classList.add("is-playing");
        button.textContent = "朗读中…";
        speak(paragraph.textContent, clearPlaying);
      });
    });

    window.addEventListener("pagehide", function () {
      synth.cancel();
    });
  }

  /* ── 返回顶部浮动按钮 ── */
  var backToTop = document.getElementById("back-to-top");
  if (backToTop) {
    var checkScroll = function () {
      var scrollY = window.scrollY
        || window.pageYOffset
        || (document.documentElement && document.documentElement.scrollTop)
        || (document.body && document.body.scrollTop)
        || 0;
      if (scrollY > 80) {
        backToTop.classList.add("is-visible");
      } else {
        backToTop.classList.remove("is-visible");
      }
    };
    window.addEventListener("scroll", checkScroll, { passive: true });
    window.addEventListener("resize", checkScroll, { passive: true });
    window.addEventListener("load", checkScroll);
    document.addEventListener("DOMContentLoaded", checkScroll);
    checkScroll();
    backToTop.addEventListener("click", function (event) {
      if (event) {
        event.preventDefault();
      }
      try {
        window.scrollTo({ top: 0, behavior: motionOK ? "smooth" : "auto" });
      } catch (error) {
        window.scrollTo(0, 0);
      }
      if (document.documentElement) {
        document.documentElement.scrollTop = 0;
      }
      if (document.body) {
        document.body.scrollTop = 0;
      }
    });
  }

  /* ── 往期归档：年月日逐级穿透日历与日期直达 ── */
  var calBox = document.getElementById("archive-calendar-box");
  if (calBox) {
    var availableDates = [];
    var datesScript = document.getElementById("archive-available-dates-json");
    if (datesScript) {
      try {
        availableDates = JSON.parse(datesScript.textContent || "[]");
      } catch (e) {
        availableDates = [];
      }
    }

    var availableDatesSet = {};
    var availableMonthsSet = {};
    var availableYearsSet = {};
    var latestYear = new Date().getFullYear();
    var latestMonth = new Date().getMonth() + 1;

    for (var dIdx = 0; dIdx < availableDates.length; dIdx++) {
      var dStr = availableDates[dIdx];
      availableDatesSet[dStr] = true;
      var yPart = dStr.substring(0, 4);
      var ymPart = dStr.substring(0, 7);
      availableYearsSet[yPart] = true;
      availableMonthsSet[ymPart] = true;
      if (dIdx === 0) {
        var pYear = parseInt(yPart, 10);
        var pMonth = parseInt(dStr.substring(5, 7), 10);
        if (!isNaN(pYear)) { latestYear = pYear; }
        if (!isNaN(pMonth)) { latestMonth = pMonth; }
      }
    }

    var currentView = "day"; // "day" | "month" | "year"
    var curYear = latestYear;
    var curMonth = latestMonth;
    var yearRangeStart = Math.floor(curYear / 10) * 10 - 1;

    var kickerEl = document.getElementById("cal-card-kicker");
    var titleTextEl = document.getElementById("cal-title-text");
    var titleBtnEl = document.getElementById("cal-title-btn");
    var titleCaretEl = document.getElementById("cal-title-caret");
    var navPrevEl = document.getElementById("cal-nav-prev");
    var navNextEl = document.getElementById("cal-nav-next");

    var viewDaysEl = document.getElementById("cal-view-days");
    var viewMonthsEl = document.getElementById("cal-view-months");
    var viewYearsEl = document.getElementById("cal-view-years");
    var daysGridEl = document.getElementById("cal-days-grid");
    var monthsGridEl = document.getElementById("cal-months-grid");
    var yearsGridEl = document.getElementById("cal-years-grid");

    var pad2 = function (n) {
      return n < 10 ? "0" + n : String(n);
    };

    var triggerAnim = function (el, animClass) {
      if (!el) { return; }
      el.classList.remove("cal-anim-prev", "cal-anim-next", "cal-anim-drill-down", "cal-anim-drill-up");
      void el.offsetWidth;
      el.classList.add(animClass);
    };

    var renderDaysView = function () {
      if (kickerEl) { kickerEl.textContent = "MONTHLY ARCHIVE"; }
      if (titleTextEl) { titleTextEl.textContent = curYear + " 年 " + curMonth + " 月"; }
      if (titleCaretEl) { titleCaretEl.style.display = "inline"; }

      if (daysGridEl) {
        daysGridEl.innerHTML = "";
        // 0=Sun -> 6, 1=Mon -> 0, etc.
        var firstDayOfWeek = new Date(curYear, curMonth - 1, 1).getDay();
        var mondayOffset = (firstDayOfWeek + 6) % 7;
        var totalDays = new Date(curYear, curMonth, 0).getDate();

        for (var e = 0; e < mondayOffset; e++) {
          var emptyCell = document.createElement("span");
          emptyCell.className = "cal-day is-empty";
          daysGridEl.appendChild(emptyCell);
        }

        for (var dayNum = 1; dayNum <= totalDays; dayNum++) {
          var isoDate = curYear + "-" + pad2(curMonth) + "-" + pad2(dayNum);
          if (availableDatesSet[isoDate]) {
            var dayLink = document.createElement("a");
            dayLink.href = "/issues/" + isoDate + "/";
            dayLink.className = "cal-day has-edition";
            dayLink.title = isoDate;
            dayLink.innerHTML = '<span class="cal-day-num">' + dayNum + '</span><span class="cal-edition-dot" aria-hidden="true"></span>';
            daysGridEl.appendChild(dayLink);
          } else {
            var noDay = document.createElement("span");
            noDay.className = "cal-day no-edition";
            noDay.textContent = String(dayNum);
            daysGridEl.appendChild(noDay);
          }
        }
      }
    };

    var renderMonthsView = function () {
      if (kickerEl) { kickerEl.textContent = "ANNUAL ARCHIVE"; }
      if (titleTextEl) { titleTextEl.textContent = curYear + " 年"; }
      if (titleCaretEl) { titleCaretEl.style.display = "inline"; }

      if (monthsGridEl) {
        monthsGridEl.innerHTML = "";
        for (var m = 1; m <= 12; m++) {
          var ymKey = curYear + "-" + pad2(m);
          var hasEd = availableMonthsSet[ymKey];
          var mBtn = document.createElement("button");
          mBtn.type = "button";
          mBtn.className = "cal-tile" + (hasEd ? " has-edition" : " no-edition") + (m === curMonth ? " is-active" : "");
          mBtn.innerHTML = '<span class="cal-tile-label">' + m + ' 月</span>' + (hasEd ? '<span class="cal-edition-dot" aria-hidden="true"></span>' : '');
          (function (targetM) {
            mBtn.addEventListener("click", function () {
              curMonth = targetM;
              switchView("day", "drill-down");
            });
          })(m);
          monthsGridEl.appendChild(mBtn);
        }
      }
    };

    var renderYearsView = function () {
      if (kickerEl) { kickerEl.textContent = "DECADE ARCHIVE"; }
      var endYear = yearRangeStart + 11;
      if (titleTextEl) { titleTextEl.textContent = yearRangeStart + " 年 - " + endYear + " 年"; }
      if (titleCaretEl) { titleCaretEl.style.display = "none"; }

      if (yearsGridEl) {
        yearsGridEl.innerHTML = "";
        for (var y = yearRangeStart; y <= endYear; y++) {
          var yKey = String(y);
          var hasEdYear = availableYearsSet[yKey];
          var yBtn = document.createElement("button");
          yBtn.type = "button";
          yBtn.className = "cal-tile" + (hasEdYear ? " has-edition" : " no-edition") + (y === curYear ? " is-active" : "");
          yBtn.innerHTML = '<span class="cal-tile-label">' + y + '</span>' + (hasEdYear ? '<span class="cal-edition-dot" aria-hidden="true"></span>' : '');
          (function (targetY) {
            yBtn.addEventListener("click", function () {
              curYear = targetY;
              switchView("month", "drill-down");
            });
          })(y);
          yearsGridEl.appendChild(yBtn);
        }
      }
    };

    var switchView = function (view, animType) {
      currentView = view;
      if (viewDaysEl) { viewDaysEl.classList.toggle("is-active", view === "day"); }
      if (viewMonthsEl) { viewMonthsEl.classList.toggle("is-active", view === "month"); }
      if (viewYearsEl) { viewYearsEl.classList.toggle("is-active", view === "year"); }

      var targetEl = null;
      if (view === "day") {
        renderDaysView();
        targetEl = daysGridEl;
      } else if (view === "month") {
        renderMonthsView();
        targetEl = monthsGridEl;
      } else if (view === "year") {
        yearRangeStart = Math.floor(curYear / 10) * 10 - 1;
        renderYearsView();
        targetEl = yearsGridEl;
      }

      if (animType === "drill-down") {
        triggerAnim(targetEl, "cal-anim-drill-down");
      } else if (animType === "drill-up") {
        triggerAnim(targetEl, "cal-anim-drill-up");
      }
    };

    // 绑定标题向上穿透点击
    if (titleBtnEl) {
      titleBtnEl.addEventListener("click", function () {
        if (currentView === "day") {
          switchView("month", "drill-up");
        } else if (currentView === "month") {
          switchView("year", "drill-up");
        }
      });
    }

    // 绑定左右箭头切换
    if (navPrevEl) {
      navPrevEl.addEventListener("click", function () {
        var grid = null;
        if (currentView === "day") {
          curMonth--;
          if (curMonth < 1) {
            curMonth = 12;
            curYear--;
          }
          renderDaysView();
          grid = daysGridEl;
        } else if (currentView === "month") {
          curYear--;
          renderMonthsView();
          grid = monthsGridEl;
        } else if (currentView === "year") {
          yearRangeStart -= 10;
          renderYearsView();
          grid = yearsGridEl;
        }
        triggerAnim(grid, "cal-anim-prev");
      });
    }

    if (navNextEl) {
      navNextEl.addEventListener("click", function () {
        var grid = null;
        if (currentView === "day") {
          curMonth++;
          if (curMonth > 12) {
            curMonth = 1;
            curYear++;
          }
          renderDaysView();
          grid = daysGridEl;
        } else if (currentView === "month") {
          curYear++;
          renderMonthsView();
          grid = monthsGridEl;
        } else if (currentView === "year") {
          yearRangeStart += 10;
          renderYearsView();
          grid = yearsGridEl;
        }
        triggerAnim(grid, "cal-anim-next");
      });
    }

    // 初始化展示日视图
    switchView("day");

    /* ── 日期直接输入查阅 ── */
    var dateInput = document.getElementById("archive-date-input");
    var jumpBtn = document.getElementById("archive-date-jump-btn");
    var msgEl = document.getElementById("cal-dispatch-msg");

    var handleDateJump = function () {
      if (!dateInput) { return; }
      var val = (dateInput.value || "").trim();
      if (!val) {
        if (msgEl) { msgEl.textContent = "请先选择或输入要查阅的日期（如 2026-07-26）"; }
        return;
      }
      if (!/^\d{4}-\d{2}-\d{2}$/.test(val)) {
        if (msgEl) { msgEl.textContent = "日期格式有误，请输入标准格式 YYYY-MM-DD"; }
        return;
      }

      if (availableDatesSet[val]) {
        if (msgEl) { msgEl.textContent = ""; }
        window.location.href = "/issues/" + val + "/";
      } else {
        var parts = val.split("-");
        var yVal = parseInt(parts[0], 10);
        var mVal = parseInt(parts[1], 10);
        if (!isNaN(yVal) && !isNaN(mVal) && mVal >= 1 && mVal <= 12) {
          curYear = yVal;
          curMonth = mVal;
          switchView("day");
          if (msgEl) {
            msgEl.textContent = val + " 暂无刊期，日历已为您定位到 " + yVal + " 年 " + mVal + " 月。";
          }
        } else if (msgEl) {
          msgEl.textContent = val + " 暂无刊期记录。";
        }
      }
    };

    if (jumpBtn) {
      jumpBtn.addEventListener("click", handleDateJump);
    }
    if (dateInput) {
      dateInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.keyCode === 13) {
          e.preventDefault();
          handleDateJump();
        }
      });
    }
  }

  /* ── 会员订阅：智谱风格固定弹窗选择支付方式 ── */
  var payModal = document.getElementById("pay-modal");
  var payModalClose = document.getElementById("pay-modal-close");
  var payModalPlanInput = document.getElementById("pay-modal-plan-input");
  var payModalTitle = document.getElementById("pay-modal-title");
  var payModalPlanPrice = document.getElementById("pay-modal-plan-price");
  var payModalOriginal = document.getElementById("pay-modal-original");
  var payModalDiscount = document.getElementById("pay-modal-discount");
  var payRowOriginal = document.getElementById("pay-row-original");
  var payRowDiscount = document.getElementById("pay-row-discount");

  var openPayModal = function (plan, planName, planPrice, originalPrice, discountLabel) {
    if (!payModal) { return; }
    if (payModalPlanInput) { payModalPlanInput.value = plan; }
    if (payModalTitle) { payModalTitle.textContent = planName || "会员订阅"; }
    if (payModalPlanPrice) { payModalPlanPrice.textContent = planPrice || ""; }

    if (originalPrice && payModalOriginal && payRowOriginal) {
      payModalOriginal.textContent = originalPrice;
      payRowOriginal.style.display = "flex";
    } else if (payRowOriginal) {
      payRowOriginal.style.display = "none";
    }

    if (discountLabel && discountLabel !== "无" && payModalDiscount && payRowDiscount) {
      payModalDiscount.textContent = discountLabel;
      payRowDiscount.style.display = "flex";
    } else if (payRowDiscount) {
      payRowDiscount.style.display = "none";
    }

    payModal.style.display = "flex";
    payModal.setAttribute("aria-hidden", "false");
  };

  var closePayModal = function () {
    if (!payModal) { return; }
    payModal.style.display = "none";
    payModal.setAttribute("aria-hidden", "true");
  };

  var subscribeButtons = document.querySelectorAll(".plan-subscribe-btn");
  for (var sIdx = 0; sIdx < subscribeButtons.length; sIdx++) {
    (function (btn) {
      btn.addEventListener("click", function (e) {
        if (e) { e.preventDefault(); }
        var plan = btn.getAttribute("data-plan") || "monthly";
        var planName = btn.getAttribute("data-plan-name") || "会员订阅";
        var planPrice = btn.getAttribute("data-plan-price") || "";
        var origPrice = btn.getAttribute("data-plan-original") || "";
        var discVal = btn.getAttribute("data-plan-discount") || "";
        openPayModal(plan, planName, planPrice, origPrice, discVal);
      });
    })(subscribeButtons[sIdx]);
  }

  if (payModalClose) {
    payModalClose.addEventListener("click", function (e) {
      if (e) {
        e.preventDefault();
        e.stopPropagation();
      }
      closePayModal();
    });
  }

  if (payModal) {
    payModal.addEventListener("click", function (e) {
      if (e.target === payModal) {
        closePayModal();
      }
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && payModal && payModal.style.display === "flex") {
      closePayModal();
    }
  });
})();
