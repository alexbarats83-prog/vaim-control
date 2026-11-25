const menu = document.querySelector(".mobile-toggle");

if (menu) {
  const sidebar = document.querySelector("#sidebar");
  const setMenu = (expanded) => {
    menu.setAttribute("aria-expanded", String(expanded));
    sidebar.classList.toggle("open", expanded);
  };
  menu.addEventListener("click", () => setMenu(menu.getAttribute("aria-expanded") === "false"));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && menu.getAttribute("aria-expanded") === "true") {
      setMenu(false);
      menu.focus();
    }
  });
}

const editor = document.querySelector("[data-route-editor]");

if (editor) {
  const steps = editor.querySelector("[data-steps]");
  const add = editor.querySelector("[data-add-step]");
  const template = editor.querySelector("[data-step-template]");
  const update = () => {
    const rows = [...steps.children];
    rows.forEach((row, index) => {
      row.querySelector('[data-move="up"]').disabled = index === 0;
      row.querySelector('[data-move="down"]').disabled = index === rows.length - 1;
      row.querySelector('[data-move="remove"]').disabled = rows.length === 1;
    });
    add.disabled = rows.length >= 20;
  };
  add.addEventListener("click", () => {
    steps.append(template.content.cloneNode(true));
    steps.lastElementChild.querySelector("input").focus();
    update();
  });
  steps.addEventListener("click", (event) => {
    const action = event.target.closest("[data-move]");
    if (!action) return;
    const row = action.closest(".route-row");
    if (action.dataset.move === "up") {
      row.previousElementSibling?.before(row);
    } else if (action.dataset.move === "down") {
      row.nextElementSibling?.after(row);
    } else {
      const neighbor = row.nextElementSibling ?? row.previousElementSibling;
      row.remove();
      neighbor?.querySelector("input").focus();
    }
    update();
    if (row.isConnected) row.querySelector("input").focus();
  });
  update();
}
