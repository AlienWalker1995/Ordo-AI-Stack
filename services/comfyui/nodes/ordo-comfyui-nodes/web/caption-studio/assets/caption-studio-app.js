function Zu(e) {
  return e && e.__esModule && Object.prototype.hasOwnProperty.call(e, "default") ? e.default : e;
}
var qu = { exports: {} }, O = {};
/**
 * @license React
 * react.production.min.js
 *
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
var wr = Symbol.for("react.element"), Ef = Symbol.for("react.portal"), Ff = Symbol.for("react.fragment"), Nf = Symbol.for("react.strict_mode"), Tf = Symbol.for("react.profiler"), Pf = Symbol.for("react.provider"), zf = Symbol.for("react.context"), Mf = Symbol.for("react.forward_ref"), Lf = Symbol.for("react.suspense"), Rf = Symbol.for("react.memo"), If = Symbol.for("react.lazy"), Ts = Symbol.iterator;
function Of(e) {
  return e === null || typeof e != "object" ? null : (e = Ts && e[Ts] || e["@@iterator"], typeof e == "function" ? e : null);
}
var bu = { isMounted: function() {
  return !1;
}, enqueueForceUpdate: function() {
}, enqueueReplaceState: function() {
}, enqueueSetState: function() {
} }, ea = Object.assign, ta = {};
function Tn(e, t, n) {
  this.props = e, this.context = t, this.refs = ta, this.updater = n || bu;
}
Tn.prototype.isReactComponent = {};
Tn.prototype.setState = function(e, t) {
  if (typeof e != "object" && typeof e != "function" && e != null) throw Error("setState(...): takes an object of state variables to update or a function which returns an object of state variables.");
  this.updater.enqueueSetState(this, e, t, "setState");
};
Tn.prototype.forceUpdate = function(e) {
  this.updater.enqueueForceUpdate(this, e, "forceUpdate");
};
function na() {
}
na.prototype = Tn.prototype;
function Li(e, t, n) {
  this.props = e, this.context = t, this.refs = ta, this.updater = n || bu;
}
var Ri = Li.prototype = new na();
Ri.constructor = Li;
ea(Ri, Tn.prototype);
Ri.isPureReactComponent = !0;
var Ps = Array.isArray, ra = Object.prototype.hasOwnProperty, Ii = { current: null }, la = { key: !0, ref: !0, __self: !0, __source: !0 };
function oa(e, t, n) {
  var r, l = {}, o = null, i = null;
  if (t != null) for (r in t.ref !== void 0 && (i = t.ref), t.key !== void 0 && (o = "" + t.key), t) ra.call(t, r) && !la.hasOwnProperty(r) && (l[r] = t[r]);
  var s = arguments.length - 2;
  if (s === 1) l.children = n;
  else if (1 < s) {
    for (var u = Array(s), a = 0; a < s; a++) u[a] = arguments[a + 2];
    l.children = u;
  }
  if (e && e.defaultProps) for (r in s = e.defaultProps, s) l[r] === void 0 && (l[r] = s[r]);
  return { $$typeof: wr, type: e, key: o, ref: i, props: l, _owner: Ii.current };
}
function Df(e, t) {
  return { $$typeof: wr, type: e.type, key: t, ref: e.ref, props: e.props, _owner: e._owner };
}
function Oi(e) {
  return typeof e == "object" && e !== null && e.$$typeof === wr;
}
function Wf(e) {
  var t = { "=": "=0", ":": "=2" };
  return "$" + e.replace(/[=:]/g, function(n) {
    return t[n];
  });
}
var zs = /\/+/g;
function Zl(e, t) {
  return typeof e == "object" && e !== null && e.key != null ? Wf("" + e.key) : t.toString(36);
}
function Vr(e, t, n, r, l) {
  var o = typeof e;
  (o === "undefined" || o === "boolean") && (e = null);
  var i = !1;
  if (e === null) i = !0;
  else switch (o) {
    case "string":
    case "number":
      i = !0;
      break;
    case "object":
      switch (e.$$typeof) {
        case wr:
        case Ef:
          i = !0;
      }
  }
  if (i) return i = e, l = l(i), e = r === "" ? "." + Zl(i, 0) : r, Ps(l) ? (n = "", e != null && (n = e.replace(zs, "$&/") + "/"), Vr(l, t, n, "", function(a) {
    return a;
  })) : l != null && (Oi(l) && (l = Df(l, n + (!l.key || i && i.key === l.key ? "" : ("" + l.key).replace(zs, "$&/") + "/") + e)), t.push(l)), 1;
  if (i = 0, r = r === "" ? "." : r + ":", Ps(e)) for (var s = 0; s < e.length; s++) {
    o = e[s];
    var u = r + Zl(o, s);
    i += Vr(o, t, n, u, l);
  }
  else if (u = Of(e), typeof u == "function") for (e = u.call(e), s = 0; !(o = e.next()).done; ) o = o.value, u = r + Zl(o, s++), i += Vr(o, t, n, u, l);
  else if (o === "object") throw t = String(e), Error("Objects are not valid as a React child (found: " + (t === "[object Object]" ? "object with keys {" + Object.keys(e).join(", ") + "}" : t) + "). If you meant to render a collection of children, use an array instead.");
  return i;
}
function Er(e, t, n) {
  if (e == null) return e;
  var r = [], l = 0;
  return Vr(e, r, "", "", function(o) {
    return t.call(n, o, l++);
  }), r;
}
function Af(e) {
  if (e._status === -1) {
    var t = e._result;
    t = t(), t.then(function(n) {
      (e._status === 0 || e._status === -1) && (e._status = 1, e._result = n);
    }, function(n) {
      (e._status === 0 || e._status === -1) && (e._status = 2, e._result = n);
    }), e._status === -1 && (e._status = 0, e._result = t);
  }
  if (e._status === 1) return e._result.default;
  throw e._result;
}
var xe = { current: null }, Qr = { transition: null }, Uf = { ReactCurrentDispatcher: xe, ReactCurrentBatchConfig: Qr, ReactCurrentOwner: Ii };
function ia() {
  throw Error("act(...) is not supported in production builds of React.");
}
O.Children = { map: Er, forEach: function(e, t, n) {
  Er(e, function() {
    t.apply(this, arguments);
  }, n);
}, count: function(e) {
  var t = 0;
  return Er(e, function() {
    t++;
  }), t;
}, toArray: function(e) {
  return Er(e, function(t) {
    return t;
  }) || [];
}, only: function(e) {
  if (!Oi(e)) throw Error("React.Children.only expected to receive a single React element child.");
  return e;
} };
O.Component = Tn;
O.Fragment = Ff;
O.Profiler = Tf;
O.PureComponent = Li;
O.StrictMode = Nf;
O.Suspense = Lf;
O.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED = Uf;
O.act = ia;
O.cloneElement = function(e, t, n) {
  if (e == null) throw Error("React.cloneElement(...): The argument must be a React element, but you passed " + e + ".");
  var r = ea({}, e.props), l = e.key, o = e.ref, i = e._owner;
  if (t != null) {
    if (t.ref !== void 0 && (o = t.ref, i = Ii.current), t.key !== void 0 && (l = "" + t.key), e.type && e.type.defaultProps) var s = e.type.defaultProps;
    for (u in t) ra.call(t, u) && !la.hasOwnProperty(u) && (r[u] = t[u] === void 0 && s !== void 0 ? s[u] : t[u]);
  }
  var u = arguments.length - 2;
  if (u === 1) r.children = n;
  else if (1 < u) {
    s = Array(u);
    for (var a = 0; a < u; a++) s[a] = arguments[a + 2];
    r.children = s;
  }
  return { $$typeof: wr, type: e.type, key: l, ref: o, props: r, _owner: i };
};
O.createContext = function(e) {
  return e = { $$typeof: zf, _currentValue: e, _currentValue2: e, _threadCount: 0, Provider: null, Consumer: null, _defaultValue: null, _globalName: null }, e.Provider = { $$typeof: Pf, _context: e }, e.Consumer = e;
};
O.createElement = oa;
O.createFactory = function(e) {
  var t = oa.bind(null, e);
  return t.type = e, t;
};
O.createRef = function() {
  return { current: null };
};
O.forwardRef = function(e) {
  return { $$typeof: Mf, render: e };
};
O.isValidElement = Oi;
O.lazy = function(e) {
  return { $$typeof: If, _payload: { _status: -1, _result: e }, _init: Af };
};
O.memo = function(e, t) {
  return { $$typeof: Rf, type: e, compare: t === void 0 ? null : t };
};
O.startTransition = function(e) {
  var t = Qr.transition;
  Qr.transition = {};
  try {
    e();
  } finally {
    Qr.transition = t;
  }
};
O.unstable_act = ia;
O.useCallback = function(e, t) {
  return xe.current.useCallback(e, t);
};
O.useContext = function(e) {
  return xe.current.useContext(e);
};
O.useDebugValue = function() {
};
O.useDeferredValue = function(e) {
  return xe.current.useDeferredValue(e);
};
O.useEffect = function(e, t) {
  return xe.current.useEffect(e, t);
};
O.useId = function() {
  return xe.current.useId();
};
O.useImperativeHandle = function(e, t, n) {
  return xe.current.useImperativeHandle(e, t, n);
};
O.useInsertionEffect = function(e, t) {
  return xe.current.useInsertionEffect(e, t);
};
O.useLayoutEffect = function(e, t) {
  return xe.current.useLayoutEffect(e, t);
};
O.useMemo = function(e, t) {
  return xe.current.useMemo(e, t);
};
O.useReducer = function(e, t, n) {
  return xe.current.useReducer(e, t, n);
};
O.useRef = function(e) {
  return xe.current.useRef(e);
};
O.useState = function(e) {
  return xe.current.useState(e);
};
O.useSyncExternalStore = function(e, t, n) {
  return xe.current.useSyncExternalStore(e, t, n);
};
O.useTransition = function() {
  return xe.current.useTransition();
};
O.version = "18.3.1";
qu.exports = O;
var R = qu.exports;
const No = /* @__PURE__ */ Zu(R);
var To = {}, sa = { exports: {} }, Le = {}, ua = { exports: {} }, aa = {};
/**
 * @license React
 * scheduler.production.min.js
 *
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
(function(e) {
  function t(E, P) {
    var L = E.length;
    E.push(P);
    e: for (; 0 < L; ) {
      var H = L - 1 >>> 1, Z = E[H];
      if (0 < l(Z, P)) E[H] = P, E[L] = Z, L = H;
      else break e;
    }
  }
  function n(E) {
    return E.length === 0 ? null : E[0];
  }
  function r(E) {
    if (E.length === 0) return null;
    var P = E[0], L = E.pop();
    if (L !== P) {
      E[0] = L;
      e: for (var H = 0, Z = E.length, tt = Z >>> 1; H < tt; ) {
        var Ge = 2 * (H + 1) - 1, Rt = E[Ge], F = Ge + 1, le = E[F];
        if (0 > l(Rt, L)) F < Z && 0 > l(le, Rt) ? (E[H] = le, E[F] = L, H = F) : (E[H] = Rt, E[Ge] = L, H = Ge);
        else if (F < Z && 0 > l(le, L)) E[H] = le, E[F] = L, H = F;
        else break e;
      }
    }
    return P;
  }
  function l(E, P) {
    var L = E.sortIndex - P.sortIndex;
    return L !== 0 ? L : E.id - P.id;
  }
  if (typeof performance == "object" && typeof performance.now == "function") {
    var o = performance;
    e.unstable_now = function() {
      return o.now();
    };
  } else {
    var i = Date, s = i.now();
    e.unstable_now = function() {
      return i.now() - s;
    };
  }
  var u = [], a = [], p = 1, h = null, m = 3, x = !1, _ = !1, k = !1, I = typeof setTimeout == "function" ? setTimeout : null, f = typeof clearTimeout == "function" ? clearTimeout : null, c = typeof setImmediate < "u" ? setImmediate : null;
  typeof navigator < "u" && navigator.scheduling !== void 0 && navigator.scheduling.isInputPending !== void 0 && navigator.scheduling.isInputPending.bind(navigator.scheduling);
  function d(E) {
    for (var P = n(a); P !== null; ) {
      if (P.callback === null) r(a);
      else if (P.startTime <= E) r(a), P.sortIndex = P.expirationTime, t(u, P);
      else break;
      P = n(a);
    }
  }
  function y(E) {
    if (k = !1, d(E), !_) if (n(u) !== null) _ = !0, fe(S);
    else {
      var P = n(a);
      P !== null && de(y, P.startTime - E);
    }
  }
  function S(E, P) {
    _ = !1, k && (k = !1, f(C), C = -1), x = !0;
    var L = m;
    try {
      for (d(P), h = n(u); h !== null && (!(h.expirationTime > P) || E && !U()); ) {
        var H = h.callback;
        if (typeof H == "function") {
          h.callback = null, m = h.priorityLevel;
          var Z = H(h.expirationTime <= P);
          P = e.unstable_now(), typeof Z == "function" ? h.callback = Z : h === n(u) && r(u), d(P);
        } else r(u);
        h = n(u);
      }
      if (h !== null) var tt = !0;
      else {
        var Ge = n(a);
        Ge !== null && de(y, Ge.startTime - P), tt = !1;
      }
      return tt;
    } finally {
      h = null, m = L, x = !1;
    }
  }
  var w = !1, g = null, C = -1, z = 5, M = -1;
  function U() {
    return !(e.unstable_now() - M < z);
  }
  function ce() {
    if (g !== null) {
      var E = e.unstable_now();
      M = E;
      var P = !0;
      try {
        P = g(!0, E);
      } finally {
        P ? $() : (w = !1, g = null);
      }
    } else w = !1;
  }
  var $;
  if (typeof c == "function") $ = function() {
    c(ce);
  };
  else if (typeof MessageChannel < "u") {
    var V = new MessageChannel(), Fe = V.port2;
    V.port1.onmessage = ce, $ = function() {
      Fe.postMessage(null);
    };
  } else $ = function() {
    I(ce, 0);
  };
  function fe(E) {
    g = E, w || (w = !0, $());
  }
  function de(E, P) {
    C = I(function() {
      E(e.unstable_now());
    }, P);
  }
  e.unstable_IdlePriority = 5, e.unstable_ImmediatePriority = 1, e.unstable_LowPriority = 4, e.unstable_NormalPriority = 3, e.unstable_Profiling = null, e.unstable_UserBlockingPriority = 2, e.unstable_cancelCallback = function(E) {
    E.callback = null;
  }, e.unstable_continueExecution = function() {
    _ || x || (_ = !0, fe(S));
  }, e.unstable_forceFrameRate = function(E) {
    0 > E || 125 < E ? console.error("forceFrameRate takes a positive int between 0 and 125, forcing frame rates higher than 125 fps is not supported") : z = 0 < E ? Math.floor(1e3 / E) : 5;
  }, e.unstable_getCurrentPriorityLevel = function() {
    return m;
  }, e.unstable_getFirstCallbackNode = function() {
    return n(u);
  }, e.unstable_next = function(E) {
    switch (m) {
      case 1:
      case 2:
      case 3:
        var P = 3;
        break;
      default:
        P = m;
    }
    var L = m;
    m = P;
    try {
      return E();
    } finally {
      m = L;
    }
  }, e.unstable_pauseExecution = function() {
  }, e.unstable_requestPaint = function() {
  }, e.unstable_runWithPriority = function(E, P) {
    switch (E) {
      case 1:
      case 2:
      case 3:
      case 4:
      case 5:
        break;
      default:
        E = 3;
    }
    var L = m;
    m = E;
    try {
      return P();
    } finally {
      m = L;
    }
  }, e.unstable_scheduleCallback = function(E, P, L) {
    var H = e.unstable_now();
    switch (typeof L == "object" && L !== null ? (L = L.delay, L = typeof L == "number" && 0 < L ? H + L : H) : L = H, E) {
      case 1:
        var Z = -1;
        break;
      case 2:
        Z = 250;
        break;
      case 5:
        Z = 1073741823;
        break;
      case 4:
        Z = 1e4;
        break;
      default:
        Z = 5e3;
    }
    return Z = L + Z, E = { id: p++, callback: P, priorityLevel: E, startTime: L, expirationTime: Z, sortIndex: -1 }, L > H ? (E.sortIndex = L, t(a, E), n(u) === null && E === n(a) && (k ? (f(C), C = -1) : k = !0, de(y, L - H))) : (E.sortIndex = Z, t(u, E), _ || x || (_ = !0, fe(S))), E;
  }, e.unstable_shouldYield = U, e.unstable_wrapCallback = function(E) {
    var P = m;
    return function() {
      var L = m;
      m = P;
      try {
        return E.apply(this, arguments);
      } finally {
        m = L;
      }
    };
  };
})(aa);
ua.exports = aa;
var $f = ua.exports;
/**
 * @license React
 * react-dom.production.min.js
 *
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
var Hf = R, ze = $f;
function j(e) {
  for (var t = "https://reactjs.org/docs/error-decoder.html?invariant=" + e, n = 1; n < arguments.length; n++) t += "&args[]=" + encodeURIComponent(arguments[n]);
  return "Minified React error #" + e + "; visit " + t + " for the full message or use the non-minified dev environment for full errors and additional helpful warnings.";
}
var ca = /* @__PURE__ */ new Set(), er = {};
function Jt(e, t) {
  wn(e, t), wn(e + "Capture", t);
}
function wn(e, t) {
  for (er[e] = t, e = 0; e < t.length; e++) ca.add(t[e]);
}
var ut = !(typeof window > "u" || typeof window.document > "u" || typeof window.document.createElement > "u"), Po = Object.prototype.hasOwnProperty, Bf = /^[:A-Z_a-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD][:A-Z_a-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD\-.0-9\u00B7\u0300-\u036F\u203F-\u2040]*$/, Ms = {}, Ls = {};
function Vf(e) {
  return Po.call(Ls, e) ? !0 : Po.call(Ms, e) ? !1 : Bf.test(e) ? Ls[e] = !0 : (Ms[e] = !0, !1);
}
function Qf(e, t, n, r) {
  if (n !== null && n.type === 0) return !1;
  switch (typeof t) {
    case "function":
    case "symbol":
      return !0;
    case "boolean":
      return r ? !1 : n !== null ? !n.acceptsBooleans : (e = e.toLowerCase().slice(0, 5), e !== "data-" && e !== "aria-");
    default:
      return !1;
  }
}
function Kf(e, t, n, r) {
  if (t === null || typeof t > "u" || Qf(e, t, n, r)) return !0;
  if (r) return !1;
  if (n !== null) switch (n.type) {
    case 3:
      return !t;
    case 4:
      return t === !1;
    case 5:
      return isNaN(t);
    case 6:
      return isNaN(t) || 1 > t;
  }
  return !1;
}
function we(e, t, n, r, l, o, i) {
  this.acceptsBooleans = t === 2 || t === 3 || t === 4, this.attributeName = r, this.attributeNamespace = l, this.mustUseProperty = n, this.propertyName = e, this.type = t, this.sanitizeURL = o, this.removeEmptyString = i;
}
var ae = {};
"children dangerouslySetInnerHTML defaultValue defaultChecked innerHTML suppressContentEditableWarning suppressHydrationWarning style".split(" ").forEach(function(e) {
  ae[e] = new we(e, 0, !1, e, null, !1, !1);
});
[["acceptCharset", "accept-charset"], ["className", "class"], ["htmlFor", "for"], ["httpEquiv", "http-equiv"]].forEach(function(e) {
  var t = e[0];
  ae[t] = new we(t, 1, !1, e[1], null, !1, !1);
});
["contentEditable", "draggable", "spellCheck", "value"].forEach(function(e) {
  ae[e] = new we(e, 2, !1, e.toLowerCase(), null, !1, !1);
});
["autoReverse", "externalResourcesRequired", "focusable", "preserveAlpha"].forEach(function(e) {
  ae[e] = new we(e, 2, !1, e, null, !1, !1);
});
"allowFullScreen async autoFocus autoPlay controls default defer disabled disablePictureInPicture disableRemotePlayback formNoValidate hidden loop noModule noValidate open playsInline readOnly required reversed scoped seamless itemScope".split(" ").forEach(function(e) {
  ae[e] = new we(e, 3, !1, e.toLowerCase(), null, !1, !1);
});
["checked", "multiple", "muted", "selected"].forEach(function(e) {
  ae[e] = new we(e, 3, !0, e, null, !1, !1);
});
["capture", "download"].forEach(function(e) {
  ae[e] = new we(e, 4, !1, e, null, !1, !1);
});
["cols", "rows", "size", "span"].forEach(function(e) {
  ae[e] = new we(e, 6, !1, e, null, !1, !1);
});
["rowSpan", "start"].forEach(function(e) {
  ae[e] = new we(e, 5, !1, e.toLowerCase(), null, !1, !1);
});
var Di = /[\-:]([a-z])/g;
function Wi(e) {
  return e[1].toUpperCase();
}
"accent-height alignment-baseline arabic-form baseline-shift cap-height clip-path clip-rule color-interpolation color-interpolation-filters color-profile color-rendering dominant-baseline enable-background fill-opacity fill-rule flood-color flood-opacity font-family font-size font-size-adjust font-stretch font-style font-variant font-weight glyph-name glyph-orientation-horizontal glyph-orientation-vertical horiz-adv-x horiz-origin-x image-rendering letter-spacing lighting-color marker-end marker-mid marker-start overline-position overline-thickness paint-order panose-1 pointer-events rendering-intent shape-rendering stop-color stop-opacity strikethrough-position strikethrough-thickness stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit stroke-opacity stroke-width text-anchor text-decoration text-rendering underline-position underline-thickness unicode-bidi unicode-range units-per-em v-alphabetic v-hanging v-ideographic v-mathematical vector-effect vert-adv-y vert-origin-x vert-origin-y word-spacing writing-mode xmlns:xlink x-height".split(" ").forEach(function(e) {
  var t = e.replace(
    Di,
    Wi
  );
  ae[t] = new we(t, 1, !1, e, null, !1, !1);
});
"xlink:actuate xlink:arcrole xlink:role xlink:show xlink:title xlink:type".split(" ").forEach(function(e) {
  var t = e.replace(Di, Wi);
  ae[t] = new we(t, 1, !1, e, "http://www.w3.org/1999/xlink", !1, !1);
});
["xml:base", "xml:lang", "xml:space"].forEach(function(e) {
  var t = e.replace(Di, Wi);
  ae[t] = new we(t, 1, !1, e, "http://www.w3.org/XML/1998/namespace", !1, !1);
});
["tabIndex", "crossOrigin"].forEach(function(e) {
  ae[e] = new we(e, 1, !1, e.toLowerCase(), null, !1, !1);
});
ae.xlinkHref = new we("xlinkHref", 1, !1, "xlink:href", "http://www.w3.org/1999/xlink", !0, !1);
["src", "href", "action", "formAction"].forEach(function(e) {
  ae[e] = new we(e, 1, !1, e.toLowerCase(), null, !0, !0);
});
function Ai(e, t, n, r) {
  var l = ae.hasOwnProperty(t) ? ae[t] : null;
  (l !== null ? l.type !== 0 : r || !(2 < t.length) || t[0] !== "o" && t[0] !== "O" || t[1] !== "n" && t[1] !== "N") && (Kf(t, n, l, r) && (n = null), r || l === null ? Vf(t) && (n === null ? e.removeAttribute(t) : e.setAttribute(t, "" + n)) : l.mustUseProperty ? e[l.propertyName] = n === null ? l.type === 3 ? !1 : "" : n : (t = l.attributeName, r = l.attributeNamespace, n === null ? e.removeAttribute(t) : (l = l.type, n = l === 3 || l === 4 && n === !0 ? "" : "" + n, r ? e.setAttributeNS(r, t, n) : e.setAttribute(t, n))));
}
var dt = Hf.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED, Fr = Symbol.for("react.element"), tn = Symbol.for("react.portal"), nn = Symbol.for("react.fragment"), Ui = Symbol.for("react.strict_mode"), zo = Symbol.for("react.profiler"), fa = Symbol.for("react.provider"), da = Symbol.for("react.context"), $i = Symbol.for("react.forward_ref"), Mo = Symbol.for("react.suspense"), Lo = Symbol.for("react.suspense_list"), Hi = Symbol.for("react.memo"), ht = Symbol.for("react.lazy"), pa = Symbol.for("react.offscreen"), Rs = Symbol.iterator;
function Ln(e) {
  return e === null || typeof e != "object" ? null : (e = Rs && e[Rs] || e["@@iterator"], typeof e == "function" ? e : null);
}
var J = Object.assign, ql;
function $n(e) {
  if (ql === void 0) try {
    throw Error();
  } catch (n) {
    var t = n.stack.trim().match(/\n( *(at )?)/);
    ql = t && t[1] || "";
  }
  return `
` + ql + e;
}
var bl = !1;
function eo(e, t) {
  if (!e || bl) return "";
  bl = !0;
  var n = Error.prepareStackTrace;
  Error.prepareStackTrace = void 0;
  try {
    if (t) if (t = function() {
      throw Error();
    }, Object.defineProperty(t.prototype, "props", { set: function() {
      throw Error();
    } }), typeof Reflect == "object" && Reflect.construct) {
      try {
        Reflect.construct(t, []);
      } catch (a) {
        var r = a;
      }
      Reflect.construct(e, [], t);
    } else {
      try {
        t.call();
      } catch (a) {
        r = a;
      }
      e.call(t.prototype);
    }
    else {
      try {
        throw Error();
      } catch (a) {
        r = a;
      }
      e();
    }
  } catch (a) {
    if (a && r && typeof a.stack == "string") {
      for (var l = a.stack.split(`
`), o = r.stack.split(`
`), i = l.length - 1, s = o.length - 1; 1 <= i && 0 <= s && l[i] !== o[s]; ) s--;
      for (; 1 <= i && 0 <= s; i--, s--) if (l[i] !== o[s]) {
        if (i !== 1 || s !== 1)
          do
            if (i--, s--, 0 > s || l[i] !== o[s]) {
              var u = `
` + l[i].replace(" at new ", " at ");
              return e.displayName && u.includes("<anonymous>") && (u = u.replace("<anonymous>", e.displayName)), u;
            }
          while (1 <= i && 0 <= s);
        break;
      }
    }
  } finally {
    bl = !1, Error.prepareStackTrace = n;
  }
  return (e = e ? e.displayName || e.name : "") ? $n(e) : "";
}
function Xf(e) {
  switch (e.tag) {
    case 5:
      return $n(e.type);
    case 16:
      return $n("Lazy");
    case 13:
      return $n("Suspense");
    case 19:
      return $n("SuspenseList");
    case 0:
    case 2:
    case 15:
      return e = eo(e.type, !1), e;
    case 11:
      return e = eo(e.type.render, !1), e;
    case 1:
      return e = eo(e.type, !0), e;
    default:
      return "";
  }
}
function Ro(e) {
  if (e == null) return null;
  if (typeof e == "function") return e.displayName || e.name || null;
  if (typeof e == "string") return e;
  switch (e) {
    case nn:
      return "Fragment";
    case tn:
      return "Portal";
    case zo:
      return "Profiler";
    case Ui:
      return "StrictMode";
    case Mo:
      return "Suspense";
    case Lo:
      return "SuspenseList";
  }
  if (typeof e == "object") switch (e.$$typeof) {
    case da:
      return (e.displayName || "Context") + ".Consumer";
    case fa:
      return (e._context.displayName || "Context") + ".Provider";
    case $i:
      var t = e.render;
      return e = e.displayName, e || (e = t.displayName || t.name || "", e = e !== "" ? "ForwardRef(" + e + ")" : "ForwardRef"), e;
    case Hi:
      return t = e.displayName || null, t !== null ? t : Ro(e.type) || "Memo";
    case ht:
      t = e._payload, e = e._init;
      try {
        return Ro(e(t));
      } catch {
      }
  }
  return null;
}
function Yf(e) {
  var t = e.type;
  switch (e.tag) {
    case 24:
      return "Cache";
    case 9:
      return (t.displayName || "Context") + ".Consumer";
    case 10:
      return (t._context.displayName || "Context") + ".Provider";
    case 18:
      return "DehydratedFragment";
    case 11:
      return e = t.render, e = e.displayName || e.name || "", t.displayName || (e !== "" ? "ForwardRef(" + e + ")" : "ForwardRef");
    case 7:
      return "Fragment";
    case 5:
      return t;
    case 4:
      return "Portal";
    case 3:
      return "Root";
    case 6:
      return "Text";
    case 16:
      return Ro(t);
    case 8:
      return t === Ui ? "StrictMode" : "Mode";
    case 22:
      return "Offscreen";
    case 12:
      return "Profiler";
    case 21:
      return "Scope";
    case 13:
      return "Suspense";
    case 19:
      return "SuspenseList";
    case 25:
      return "TracingMarker";
    case 1:
    case 0:
    case 17:
    case 2:
    case 14:
    case 15:
      if (typeof t == "function") return t.displayName || t.name || null;
      if (typeof t == "string") return t;
  }
  return null;
}
function Tt(e) {
  switch (typeof e) {
    case "boolean":
    case "number":
    case "string":
    case "undefined":
      return e;
    case "object":
      return e;
    default:
      return "";
  }
}
function ma(e) {
  var t = e.type;
  return (e = e.nodeName) && e.toLowerCase() === "input" && (t === "checkbox" || t === "radio");
}
function Gf(e) {
  var t = ma(e) ? "checked" : "value", n = Object.getOwnPropertyDescriptor(e.constructor.prototype, t), r = "" + e[t];
  if (!e.hasOwnProperty(t) && typeof n < "u" && typeof n.get == "function" && typeof n.set == "function") {
    var l = n.get, o = n.set;
    return Object.defineProperty(e, t, { configurable: !0, get: function() {
      return l.call(this);
    }, set: function(i) {
      r = "" + i, o.call(this, i);
    } }), Object.defineProperty(e, t, { enumerable: n.enumerable }), { getValue: function() {
      return r;
    }, setValue: function(i) {
      r = "" + i;
    }, stopTracking: function() {
      e._valueTracker = null, delete e[t];
    } };
  }
}
function Nr(e) {
  e._valueTracker || (e._valueTracker = Gf(e));
}
function ha(e) {
  if (!e) return !1;
  var t = e._valueTracker;
  if (!t) return !0;
  var n = t.getValue(), r = "";
  return e && (r = ma(e) ? e.checked ? "true" : "false" : e.value), e = r, e !== n ? (t.setValue(e), !0) : !1;
}
function nl(e) {
  if (e = e || (typeof document < "u" ? document : void 0), typeof e > "u") return null;
  try {
    return e.activeElement || e.body;
  } catch {
    return e.body;
  }
}
function Io(e, t) {
  var n = t.checked;
  return J({}, t, { defaultChecked: void 0, defaultValue: void 0, value: void 0, checked: n ?? e._wrapperState.initialChecked });
}
function Is(e, t) {
  var n = t.defaultValue == null ? "" : t.defaultValue, r = t.checked != null ? t.checked : t.defaultChecked;
  n = Tt(t.value != null ? t.value : n), e._wrapperState = { initialChecked: r, initialValue: n, controlled: t.type === "checkbox" || t.type === "radio" ? t.checked != null : t.value != null };
}
function va(e, t) {
  t = t.checked, t != null && Ai(e, "checked", t, !1);
}
function Oo(e, t) {
  va(e, t);
  var n = Tt(t.value), r = t.type;
  if (n != null) r === "number" ? (n === 0 && e.value === "" || e.value != n) && (e.value = "" + n) : e.value !== "" + n && (e.value = "" + n);
  else if (r === "submit" || r === "reset") {
    e.removeAttribute("value");
    return;
  }
  t.hasOwnProperty("value") ? Do(e, t.type, n) : t.hasOwnProperty("defaultValue") && Do(e, t.type, Tt(t.defaultValue)), t.checked == null && t.defaultChecked != null && (e.defaultChecked = !!t.defaultChecked);
}
function Os(e, t, n) {
  if (t.hasOwnProperty("value") || t.hasOwnProperty("defaultValue")) {
    var r = t.type;
    if (!(r !== "submit" && r !== "reset" || t.value !== void 0 && t.value !== null)) return;
    t = "" + e._wrapperState.initialValue, n || t === e.value || (e.value = t), e.defaultValue = t;
  }
  n = e.name, n !== "" && (e.name = ""), e.defaultChecked = !!e._wrapperState.initialChecked, n !== "" && (e.name = n);
}
function Do(e, t, n) {
  (t !== "number" || nl(e.ownerDocument) !== e) && (n == null ? e.defaultValue = "" + e._wrapperState.initialValue : e.defaultValue !== "" + n && (e.defaultValue = "" + n));
}
var Hn = Array.isArray;
function mn(e, t, n, r) {
  if (e = e.options, t) {
    t = {};
    for (var l = 0; l < n.length; l++) t["$" + n[l]] = !0;
    for (n = 0; n < e.length; n++) l = t.hasOwnProperty("$" + e[n].value), e[n].selected !== l && (e[n].selected = l), l && r && (e[n].defaultSelected = !0);
  } else {
    for (n = "" + Tt(n), t = null, l = 0; l < e.length; l++) {
      if (e[l].value === n) {
        e[l].selected = !0, r && (e[l].defaultSelected = !0);
        return;
      }
      t !== null || e[l].disabled || (t = e[l]);
    }
    t !== null && (t.selected = !0);
  }
}
function Wo(e, t) {
  if (t.dangerouslySetInnerHTML != null) throw Error(j(91));
  return J({}, t, { value: void 0, defaultValue: void 0, children: "" + e._wrapperState.initialValue });
}
function Ds(e, t) {
  var n = t.value;
  if (n == null) {
    if (n = t.children, t = t.defaultValue, n != null) {
      if (t != null) throw Error(j(92));
      if (Hn(n)) {
        if (1 < n.length) throw Error(j(93));
        n = n[0];
      }
      t = n;
    }
    t == null && (t = ""), n = t;
  }
  e._wrapperState = { initialValue: Tt(n) };
}
function ga(e, t) {
  var n = Tt(t.value), r = Tt(t.defaultValue);
  n != null && (n = "" + n, n !== e.value && (e.value = n), t.defaultValue == null && e.defaultValue !== n && (e.defaultValue = n)), r != null && (e.defaultValue = "" + r);
}
function Ws(e) {
  var t = e.textContent;
  t === e._wrapperState.initialValue && t !== "" && t !== null && (e.value = t);
}
function ya(e) {
  switch (e) {
    case "svg":
      return "http://www.w3.org/2000/svg";
    case "math":
      return "http://www.w3.org/1998/Math/MathML";
    default:
      return "http://www.w3.org/1999/xhtml";
  }
}
function Ao(e, t) {
  return e == null || e === "http://www.w3.org/1999/xhtml" ? ya(t) : e === "http://www.w3.org/2000/svg" && t === "foreignObject" ? "http://www.w3.org/1999/xhtml" : e;
}
var Tr, xa = function(e) {
  return typeof MSApp < "u" && MSApp.execUnsafeLocalFunction ? function(t, n, r, l) {
    MSApp.execUnsafeLocalFunction(function() {
      return e(t, n, r, l);
    });
  } : e;
}(function(e, t) {
  if (e.namespaceURI !== "http://www.w3.org/2000/svg" || "innerHTML" in e) e.innerHTML = t;
  else {
    for (Tr = Tr || document.createElement("div"), Tr.innerHTML = "<svg>" + t.valueOf().toString() + "</svg>", t = Tr.firstChild; e.firstChild; ) e.removeChild(e.firstChild);
    for (; t.firstChild; ) e.appendChild(t.firstChild);
  }
});
function tr(e, t) {
  if (t) {
    var n = e.firstChild;
    if (n && n === e.lastChild && n.nodeType === 3) {
      n.nodeValue = t;
      return;
    }
  }
  e.textContent = t;
}
var Qn = {
  animationIterationCount: !0,
  aspectRatio: !0,
  borderImageOutset: !0,
  borderImageSlice: !0,
  borderImageWidth: !0,
  boxFlex: !0,
  boxFlexGroup: !0,
  boxOrdinalGroup: !0,
  columnCount: !0,
  columns: !0,
  flex: !0,
  flexGrow: !0,
  flexPositive: !0,
  flexShrink: !0,
  flexNegative: !0,
  flexOrder: !0,
  gridArea: !0,
  gridRow: !0,
  gridRowEnd: !0,
  gridRowSpan: !0,
  gridRowStart: !0,
  gridColumn: !0,
  gridColumnEnd: !0,
  gridColumnSpan: !0,
  gridColumnStart: !0,
  fontWeight: !0,
  lineClamp: !0,
  lineHeight: !0,
  opacity: !0,
  order: !0,
  orphans: !0,
  tabSize: !0,
  widows: !0,
  zIndex: !0,
  zoom: !0,
  fillOpacity: !0,
  floodOpacity: !0,
  stopOpacity: !0,
  strokeDasharray: !0,
  strokeDashoffset: !0,
  strokeMiterlimit: !0,
  strokeOpacity: !0,
  strokeWidth: !0
}, Jf = ["Webkit", "ms", "Moz", "O"];
Object.keys(Qn).forEach(function(e) {
  Jf.forEach(function(t) {
    t = t + e.charAt(0).toUpperCase() + e.substring(1), Qn[t] = Qn[e];
  });
});
function wa(e, t, n) {
  return t == null || typeof t == "boolean" || t === "" ? "" : n || typeof t != "number" || t === 0 || Qn.hasOwnProperty(e) && Qn[e] ? ("" + t).trim() : t + "px";
}
function _a(e, t) {
  e = e.style;
  for (var n in t) if (t.hasOwnProperty(n)) {
    var r = n.indexOf("--") === 0, l = wa(n, t[n], r);
    n === "float" && (n = "cssFloat"), r ? e.setProperty(n, l) : e[n] = l;
  }
}
var Zf = J({ menuitem: !0 }, { area: !0, base: !0, br: !0, col: !0, embed: !0, hr: !0, img: !0, input: !0, keygen: !0, link: !0, meta: !0, param: !0, source: !0, track: !0, wbr: !0 });
function Uo(e, t) {
  if (t) {
    if (Zf[e] && (t.children != null || t.dangerouslySetInnerHTML != null)) throw Error(j(137, e));
    if (t.dangerouslySetInnerHTML != null) {
      if (t.children != null) throw Error(j(60));
      if (typeof t.dangerouslySetInnerHTML != "object" || !("__html" in t.dangerouslySetInnerHTML)) throw Error(j(61));
    }
    if (t.style != null && typeof t.style != "object") throw Error(j(62));
  }
}
function $o(e, t) {
  if (e.indexOf("-") === -1) return typeof t.is == "string";
  switch (e) {
    case "annotation-xml":
    case "color-profile":
    case "font-face":
    case "font-face-src":
    case "font-face-uri":
    case "font-face-format":
    case "font-face-name":
    case "missing-glyph":
      return !1;
    default:
      return !0;
  }
}
var Ho = null;
function Bi(e) {
  return e = e.target || e.srcElement || window, e.correspondingUseElement && (e = e.correspondingUseElement), e.nodeType === 3 ? e.parentNode : e;
}
var Bo = null, hn = null, vn = null;
function As(e) {
  if (e = kr(e)) {
    if (typeof Bo != "function") throw Error(j(280));
    var t = e.stateNode;
    t && (t = Il(t), Bo(e.stateNode, e.type, t));
  }
}
function ja(e) {
  hn ? vn ? vn.push(e) : vn = [e] : hn = e;
}
function ka() {
  if (hn) {
    var e = hn, t = vn;
    if (vn = hn = null, As(e), t) for (e = 0; e < t.length; e++) As(t[e]);
  }
}
function Sa(e, t) {
  return e(t);
}
function Ca() {
}
var to = !1;
function Ea(e, t, n) {
  if (to) return e(t, n);
  to = !0;
  try {
    return Sa(e, t, n);
  } finally {
    to = !1, (hn !== null || vn !== null) && (Ca(), ka());
  }
}
function nr(e, t) {
  var n = e.stateNode;
  if (n === null) return null;
  var r = Il(n);
  if (r === null) return null;
  n = r[t];
  e: switch (t) {
    case "onClick":
    case "onClickCapture":
    case "onDoubleClick":
    case "onDoubleClickCapture":
    case "onMouseDown":
    case "onMouseDownCapture":
    case "onMouseMove":
    case "onMouseMoveCapture":
    case "onMouseUp":
    case "onMouseUpCapture":
    case "onMouseEnter":
      (r = !r.disabled) || (e = e.type, r = !(e === "button" || e === "input" || e === "select" || e === "textarea")), e = !r;
      break e;
    default:
      e = !1;
  }
  if (e) return null;
  if (n && typeof n != "function") throw Error(j(231, t, typeof n));
  return n;
}
var Vo = !1;
if (ut) try {
  var Rn = {};
  Object.defineProperty(Rn, "passive", { get: function() {
    Vo = !0;
  } }), window.addEventListener("test", Rn, Rn), window.removeEventListener("test", Rn, Rn);
} catch {
  Vo = !1;
}
function qf(e, t, n, r, l, o, i, s, u) {
  var a = Array.prototype.slice.call(arguments, 3);
  try {
    t.apply(n, a);
  } catch (p) {
    this.onError(p);
  }
}
var Kn = !1, rl = null, ll = !1, Qo = null, bf = { onError: function(e) {
  Kn = !0, rl = e;
} };
function ed(e, t, n, r, l, o, i, s, u) {
  Kn = !1, rl = null, qf.apply(bf, arguments);
}
function td(e, t, n, r, l, o, i, s, u) {
  if (ed.apply(this, arguments), Kn) {
    if (Kn) {
      var a = rl;
      Kn = !1, rl = null;
    } else throw Error(j(198));
    ll || (ll = !0, Qo = a);
  }
}
function Zt(e) {
  var t = e, n = e;
  if (e.alternate) for (; t.return; ) t = t.return;
  else {
    e = t;
    do
      t = e, t.flags & 4098 && (n = t.return), e = t.return;
    while (e);
  }
  return t.tag === 3 ? n : null;
}
function Fa(e) {
  if (e.tag === 13) {
    var t = e.memoizedState;
    if (t === null && (e = e.alternate, e !== null && (t = e.memoizedState)), t !== null) return t.dehydrated;
  }
  return null;
}
function Us(e) {
  if (Zt(e) !== e) throw Error(j(188));
}
function nd(e) {
  var t = e.alternate;
  if (!t) {
    if (t = Zt(e), t === null) throw Error(j(188));
    return t !== e ? null : e;
  }
  for (var n = e, r = t; ; ) {
    var l = n.return;
    if (l === null) break;
    var o = l.alternate;
    if (o === null) {
      if (r = l.return, r !== null) {
        n = r;
        continue;
      }
      break;
    }
    if (l.child === o.child) {
      for (o = l.child; o; ) {
        if (o === n) return Us(l), e;
        if (o === r) return Us(l), t;
        o = o.sibling;
      }
      throw Error(j(188));
    }
    if (n.return !== r.return) n = l, r = o;
    else {
      for (var i = !1, s = l.child; s; ) {
        if (s === n) {
          i = !0, n = l, r = o;
          break;
        }
        if (s === r) {
          i = !0, r = l, n = o;
          break;
        }
        s = s.sibling;
      }
      if (!i) {
        for (s = o.child; s; ) {
          if (s === n) {
            i = !0, n = o, r = l;
            break;
          }
          if (s === r) {
            i = !0, r = o, n = l;
            break;
          }
          s = s.sibling;
        }
        if (!i) throw Error(j(189));
      }
    }
    if (n.alternate !== r) throw Error(j(190));
  }
  if (n.tag !== 3) throw Error(j(188));
  return n.stateNode.current === n ? e : t;
}
function Na(e) {
  return e = nd(e), e !== null ? Ta(e) : null;
}
function Ta(e) {
  if (e.tag === 5 || e.tag === 6) return e;
  for (e = e.child; e !== null; ) {
    var t = Ta(e);
    if (t !== null) return t;
    e = e.sibling;
  }
  return null;
}
var Pa = ze.unstable_scheduleCallback, $s = ze.unstable_cancelCallback, rd = ze.unstable_shouldYield, ld = ze.unstable_requestPaint, ee = ze.unstable_now, od = ze.unstable_getCurrentPriorityLevel, Vi = ze.unstable_ImmediatePriority, za = ze.unstable_UserBlockingPriority, ol = ze.unstable_NormalPriority, id = ze.unstable_LowPriority, Ma = ze.unstable_IdlePriority, zl = null, be = null;
function sd(e) {
  if (be && typeof be.onCommitFiberRoot == "function") try {
    be.onCommitFiberRoot(zl, e, void 0, (e.current.flags & 128) === 128);
  } catch {
  }
}
var Ke = Math.clz32 ? Math.clz32 : cd, ud = Math.log, ad = Math.LN2;
function cd(e) {
  return e >>>= 0, e === 0 ? 32 : 31 - (ud(e) / ad | 0) | 0;
}
var Pr = 64, zr = 4194304;
function Bn(e) {
  switch (e & -e) {
    case 1:
      return 1;
    case 2:
      return 2;
    case 4:
      return 4;
    case 8:
      return 8;
    case 16:
      return 16;
    case 32:
      return 32;
    case 64:
    case 128:
    case 256:
    case 512:
    case 1024:
    case 2048:
    case 4096:
    case 8192:
    case 16384:
    case 32768:
    case 65536:
    case 131072:
    case 262144:
    case 524288:
    case 1048576:
    case 2097152:
      return e & 4194240;
    case 4194304:
    case 8388608:
    case 16777216:
    case 33554432:
    case 67108864:
      return e & 130023424;
    case 134217728:
      return 134217728;
    case 268435456:
      return 268435456;
    case 536870912:
      return 536870912;
    case 1073741824:
      return 1073741824;
    default:
      return e;
  }
}
function il(e, t) {
  var n = e.pendingLanes;
  if (n === 0) return 0;
  var r = 0, l = e.suspendedLanes, o = e.pingedLanes, i = n & 268435455;
  if (i !== 0) {
    var s = i & ~l;
    s !== 0 ? r = Bn(s) : (o &= i, o !== 0 && (r = Bn(o)));
  } else i = n & ~l, i !== 0 ? r = Bn(i) : o !== 0 && (r = Bn(o));
  if (r === 0) return 0;
  if (t !== 0 && t !== r && !(t & l) && (l = r & -r, o = t & -t, l >= o || l === 16 && (o & 4194240) !== 0)) return t;
  if (r & 4 && (r |= n & 16), t = e.entangledLanes, t !== 0) for (e = e.entanglements, t &= r; 0 < t; ) n = 31 - Ke(t), l = 1 << n, r |= e[n], t &= ~l;
  return r;
}
function fd(e, t) {
  switch (e) {
    case 1:
    case 2:
    case 4:
      return t + 250;
    case 8:
    case 16:
    case 32:
    case 64:
    case 128:
    case 256:
    case 512:
    case 1024:
    case 2048:
    case 4096:
    case 8192:
    case 16384:
    case 32768:
    case 65536:
    case 131072:
    case 262144:
    case 524288:
    case 1048576:
    case 2097152:
      return t + 5e3;
    case 4194304:
    case 8388608:
    case 16777216:
    case 33554432:
    case 67108864:
      return -1;
    case 134217728:
    case 268435456:
    case 536870912:
    case 1073741824:
      return -1;
    default:
      return -1;
  }
}
function dd(e, t) {
  for (var n = e.suspendedLanes, r = e.pingedLanes, l = e.expirationTimes, o = e.pendingLanes; 0 < o; ) {
    var i = 31 - Ke(o), s = 1 << i, u = l[i];
    u === -1 ? (!(s & n) || s & r) && (l[i] = fd(s, t)) : u <= t && (e.expiredLanes |= s), o &= ~s;
  }
}
function Ko(e) {
  return e = e.pendingLanes & -1073741825, e !== 0 ? e : e & 1073741824 ? 1073741824 : 0;
}
function La() {
  var e = Pr;
  return Pr <<= 1, !(Pr & 4194240) && (Pr = 64), e;
}
function no(e) {
  for (var t = [], n = 0; 31 > n; n++) t.push(e);
  return t;
}
function _r(e, t, n) {
  e.pendingLanes |= t, t !== 536870912 && (e.suspendedLanes = 0, e.pingedLanes = 0), e = e.eventTimes, t = 31 - Ke(t), e[t] = n;
}
function pd(e, t) {
  var n = e.pendingLanes & ~t;
  e.pendingLanes = t, e.suspendedLanes = 0, e.pingedLanes = 0, e.expiredLanes &= t, e.mutableReadLanes &= t, e.entangledLanes &= t, t = e.entanglements;
  var r = e.eventTimes;
  for (e = e.expirationTimes; 0 < n; ) {
    var l = 31 - Ke(n), o = 1 << l;
    t[l] = 0, r[l] = -1, e[l] = -1, n &= ~o;
  }
}
function Qi(e, t) {
  var n = e.entangledLanes |= t;
  for (e = e.entanglements; n; ) {
    var r = 31 - Ke(n), l = 1 << r;
    l & t | e[r] & t && (e[r] |= t), n &= ~l;
  }
}
var A = 0;
function Ra(e) {
  return e &= -e, 1 < e ? 4 < e ? e & 268435455 ? 16 : 536870912 : 4 : 1;
}
var Ia, Ki, Oa, Da, Wa, Xo = !1, Mr = [], _t = null, jt = null, kt = null, rr = /* @__PURE__ */ new Map(), lr = /* @__PURE__ */ new Map(), gt = [], md = "mousedown mouseup touchcancel touchend touchstart auxclick dblclick pointercancel pointerdown pointerup dragend dragstart drop compositionend compositionstart keydown keypress keyup input textInput copy cut paste click change contextmenu reset submit".split(" ");
function Hs(e, t) {
  switch (e) {
    case "focusin":
    case "focusout":
      _t = null;
      break;
    case "dragenter":
    case "dragleave":
      jt = null;
      break;
    case "mouseover":
    case "mouseout":
      kt = null;
      break;
    case "pointerover":
    case "pointerout":
      rr.delete(t.pointerId);
      break;
    case "gotpointercapture":
    case "lostpointercapture":
      lr.delete(t.pointerId);
  }
}
function In(e, t, n, r, l, o) {
  return e === null || e.nativeEvent !== o ? (e = { blockedOn: t, domEventName: n, eventSystemFlags: r, nativeEvent: o, targetContainers: [l] }, t !== null && (t = kr(t), t !== null && Ki(t)), e) : (e.eventSystemFlags |= r, t = e.targetContainers, l !== null && t.indexOf(l) === -1 && t.push(l), e);
}
function hd(e, t, n, r, l) {
  switch (t) {
    case "focusin":
      return _t = In(_t, e, t, n, r, l), !0;
    case "dragenter":
      return jt = In(jt, e, t, n, r, l), !0;
    case "mouseover":
      return kt = In(kt, e, t, n, r, l), !0;
    case "pointerover":
      var o = l.pointerId;
      return rr.set(o, In(rr.get(o) || null, e, t, n, r, l)), !0;
    case "gotpointercapture":
      return o = l.pointerId, lr.set(o, In(lr.get(o) || null, e, t, n, r, l)), !0;
  }
  return !1;
}
function Aa(e) {
  var t = Wt(e.target);
  if (t !== null) {
    var n = Zt(t);
    if (n !== null) {
      if (t = n.tag, t === 13) {
        if (t = Fa(n), t !== null) {
          e.blockedOn = t, Wa(e.priority, function() {
            Oa(n);
          });
          return;
        }
      } else if (t === 3 && n.stateNode.current.memoizedState.isDehydrated) {
        e.blockedOn = n.tag === 3 ? n.stateNode.containerInfo : null;
        return;
      }
    }
  }
  e.blockedOn = null;
}
function Kr(e) {
  if (e.blockedOn !== null) return !1;
  for (var t = e.targetContainers; 0 < t.length; ) {
    var n = Yo(e.domEventName, e.eventSystemFlags, t[0], e.nativeEvent);
    if (n === null) {
      n = e.nativeEvent;
      var r = new n.constructor(n.type, n);
      Ho = r, n.target.dispatchEvent(r), Ho = null;
    } else return t = kr(n), t !== null && Ki(t), e.blockedOn = n, !1;
    t.shift();
  }
  return !0;
}
function Bs(e, t, n) {
  Kr(e) && n.delete(t);
}
function vd() {
  Xo = !1, _t !== null && Kr(_t) && (_t = null), jt !== null && Kr(jt) && (jt = null), kt !== null && Kr(kt) && (kt = null), rr.forEach(Bs), lr.forEach(Bs);
}
function On(e, t) {
  e.blockedOn === t && (e.blockedOn = null, Xo || (Xo = !0, ze.unstable_scheduleCallback(ze.unstable_NormalPriority, vd)));
}
function or(e) {
  function t(l) {
    return On(l, e);
  }
  if (0 < Mr.length) {
    On(Mr[0], e);
    for (var n = 1; n < Mr.length; n++) {
      var r = Mr[n];
      r.blockedOn === e && (r.blockedOn = null);
    }
  }
  for (_t !== null && On(_t, e), jt !== null && On(jt, e), kt !== null && On(kt, e), rr.forEach(t), lr.forEach(t), n = 0; n < gt.length; n++) r = gt[n], r.blockedOn === e && (r.blockedOn = null);
  for (; 0 < gt.length && (n = gt[0], n.blockedOn === null); ) Aa(n), n.blockedOn === null && gt.shift();
}
var gn = dt.ReactCurrentBatchConfig, sl = !0;
function gd(e, t, n, r) {
  var l = A, o = gn.transition;
  gn.transition = null;
  try {
    A = 1, Xi(e, t, n, r);
  } finally {
    A = l, gn.transition = o;
  }
}
function yd(e, t, n, r) {
  var l = A, o = gn.transition;
  gn.transition = null;
  try {
    A = 4, Xi(e, t, n, r);
  } finally {
    A = l, gn.transition = o;
  }
}
function Xi(e, t, n, r) {
  if (sl) {
    var l = Yo(e, t, n, r);
    if (l === null) po(e, t, r, ul, n), Hs(e, r);
    else if (hd(l, e, t, n, r)) r.stopPropagation();
    else if (Hs(e, r), t & 4 && -1 < md.indexOf(e)) {
      for (; l !== null; ) {
        var o = kr(l);
        if (o !== null && Ia(o), o = Yo(e, t, n, r), o === null && po(e, t, r, ul, n), o === l) break;
        l = o;
      }
      l !== null && r.stopPropagation();
    } else po(e, t, r, null, n);
  }
}
var ul = null;
function Yo(e, t, n, r) {
  if (ul = null, e = Bi(r), e = Wt(e), e !== null) if (t = Zt(e), t === null) e = null;
  else if (n = t.tag, n === 13) {
    if (e = Fa(t), e !== null) return e;
    e = null;
  } else if (n === 3) {
    if (t.stateNode.current.memoizedState.isDehydrated) return t.tag === 3 ? t.stateNode.containerInfo : null;
    e = null;
  } else t !== e && (e = null);
  return ul = e, null;
}
function Ua(e) {
  switch (e) {
    case "cancel":
    case "click":
    case "close":
    case "contextmenu":
    case "copy":
    case "cut":
    case "auxclick":
    case "dblclick":
    case "dragend":
    case "dragstart":
    case "drop":
    case "focusin":
    case "focusout":
    case "input":
    case "invalid":
    case "keydown":
    case "keypress":
    case "keyup":
    case "mousedown":
    case "mouseup":
    case "paste":
    case "pause":
    case "play":
    case "pointercancel":
    case "pointerdown":
    case "pointerup":
    case "ratechange":
    case "reset":
    case "resize":
    case "seeked":
    case "submit":
    case "touchcancel":
    case "touchend":
    case "touchstart":
    case "volumechange":
    case "change":
    case "selectionchange":
    case "textInput":
    case "compositionstart":
    case "compositionend":
    case "compositionupdate":
    case "beforeblur":
    case "afterblur":
    case "beforeinput":
    case "blur":
    case "fullscreenchange":
    case "focus":
    case "hashchange":
    case "popstate":
    case "select":
    case "selectstart":
      return 1;
    case "drag":
    case "dragenter":
    case "dragexit":
    case "dragleave":
    case "dragover":
    case "mousemove":
    case "mouseout":
    case "mouseover":
    case "pointermove":
    case "pointerout":
    case "pointerover":
    case "scroll":
    case "toggle":
    case "touchmove":
    case "wheel":
    case "mouseenter":
    case "mouseleave":
    case "pointerenter":
    case "pointerleave":
      return 4;
    case "message":
      switch (od()) {
        case Vi:
          return 1;
        case za:
          return 4;
        case ol:
        case id:
          return 16;
        case Ma:
          return 536870912;
        default:
          return 16;
      }
    default:
      return 16;
  }
}
var xt = null, Yi = null, Xr = null;
function $a() {
  if (Xr) return Xr;
  var e, t = Yi, n = t.length, r, l = "value" in xt ? xt.value : xt.textContent, o = l.length;
  for (e = 0; e < n && t[e] === l[e]; e++) ;
  var i = n - e;
  for (r = 1; r <= i && t[n - r] === l[o - r]; r++) ;
  return Xr = l.slice(e, 1 < r ? 1 - r : void 0);
}
function Yr(e) {
  var t = e.keyCode;
  return "charCode" in e ? (e = e.charCode, e === 0 && t === 13 && (e = 13)) : e = t, e === 10 && (e = 13), 32 <= e || e === 13 ? e : 0;
}
function Lr() {
  return !0;
}
function Vs() {
  return !1;
}
function Re(e) {
  function t(n, r, l, o, i) {
    this._reactName = n, this._targetInst = l, this.type = r, this.nativeEvent = o, this.target = i, this.currentTarget = null;
    for (var s in e) e.hasOwnProperty(s) && (n = e[s], this[s] = n ? n(o) : o[s]);
    return this.isDefaultPrevented = (o.defaultPrevented != null ? o.defaultPrevented : o.returnValue === !1) ? Lr : Vs, this.isPropagationStopped = Vs, this;
  }
  return J(t.prototype, { preventDefault: function() {
    this.defaultPrevented = !0;
    var n = this.nativeEvent;
    n && (n.preventDefault ? n.preventDefault() : typeof n.returnValue != "unknown" && (n.returnValue = !1), this.isDefaultPrevented = Lr);
  }, stopPropagation: function() {
    var n = this.nativeEvent;
    n && (n.stopPropagation ? n.stopPropagation() : typeof n.cancelBubble != "unknown" && (n.cancelBubble = !0), this.isPropagationStopped = Lr);
  }, persist: function() {
  }, isPersistent: Lr }), t;
}
var Pn = { eventPhase: 0, bubbles: 0, cancelable: 0, timeStamp: function(e) {
  return e.timeStamp || Date.now();
}, defaultPrevented: 0, isTrusted: 0 }, Gi = Re(Pn), jr = J({}, Pn, { view: 0, detail: 0 }), xd = Re(jr), ro, lo, Dn, Ml = J({}, jr, { screenX: 0, screenY: 0, clientX: 0, clientY: 0, pageX: 0, pageY: 0, ctrlKey: 0, shiftKey: 0, altKey: 0, metaKey: 0, getModifierState: Ji, button: 0, buttons: 0, relatedTarget: function(e) {
  return e.relatedTarget === void 0 ? e.fromElement === e.srcElement ? e.toElement : e.fromElement : e.relatedTarget;
}, movementX: function(e) {
  return "movementX" in e ? e.movementX : (e !== Dn && (Dn && e.type === "mousemove" ? (ro = e.screenX - Dn.screenX, lo = e.screenY - Dn.screenY) : lo = ro = 0, Dn = e), ro);
}, movementY: function(e) {
  return "movementY" in e ? e.movementY : lo;
} }), Qs = Re(Ml), wd = J({}, Ml, { dataTransfer: 0 }), _d = Re(wd), jd = J({}, jr, { relatedTarget: 0 }), oo = Re(jd), kd = J({}, Pn, { animationName: 0, elapsedTime: 0, pseudoElement: 0 }), Sd = Re(kd), Cd = J({}, Pn, { clipboardData: function(e) {
  return "clipboardData" in e ? e.clipboardData : window.clipboardData;
} }), Ed = Re(Cd), Fd = J({}, Pn, { data: 0 }), Ks = Re(Fd), Nd = {
  Esc: "Escape",
  Spacebar: " ",
  Left: "ArrowLeft",
  Up: "ArrowUp",
  Right: "ArrowRight",
  Down: "ArrowDown",
  Del: "Delete",
  Win: "OS",
  Menu: "ContextMenu",
  Apps: "ContextMenu",
  Scroll: "ScrollLock",
  MozPrintableKey: "Unidentified"
}, Td = {
  8: "Backspace",
  9: "Tab",
  12: "Clear",
  13: "Enter",
  16: "Shift",
  17: "Control",
  18: "Alt",
  19: "Pause",
  20: "CapsLock",
  27: "Escape",
  32: " ",
  33: "PageUp",
  34: "PageDown",
  35: "End",
  36: "Home",
  37: "ArrowLeft",
  38: "ArrowUp",
  39: "ArrowRight",
  40: "ArrowDown",
  45: "Insert",
  46: "Delete",
  112: "F1",
  113: "F2",
  114: "F3",
  115: "F4",
  116: "F5",
  117: "F6",
  118: "F7",
  119: "F8",
  120: "F9",
  121: "F10",
  122: "F11",
  123: "F12",
  144: "NumLock",
  145: "ScrollLock",
  224: "Meta"
}, Pd = { Alt: "altKey", Control: "ctrlKey", Meta: "metaKey", Shift: "shiftKey" };
function zd(e) {
  var t = this.nativeEvent;
  return t.getModifierState ? t.getModifierState(e) : (e = Pd[e]) ? !!t[e] : !1;
}
function Ji() {
  return zd;
}
var Md = J({}, jr, { key: function(e) {
  if (e.key) {
    var t = Nd[e.key] || e.key;
    if (t !== "Unidentified") return t;
  }
  return e.type === "keypress" ? (e = Yr(e), e === 13 ? "Enter" : String.fromCharCode(e)) : e.type === "keydown" || e.type === "keyup" ? Td[e.keyCode] || "Unidentified" : "";
}, code: 0, location: 0, ctrlKey: 0, shiftKey: 0, altKey: 0, metaKey: 0, repeat: 0, locale: 0, getModifierState: Ji, charCode: function(e) {
  return e.type === "keypress" ? Yr(e) : 0;
}, keyCode: function(e) {
  return e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
}, which: function(e) {
  return e.type === "keypress" ? Yr(e) : e.type === "keydown" || e.type === "keyup" ? e.keyCode : 0;
} }), Ld = Re(Md), Rd = J({}, Ml, { pointerId: 0, width: 0, height: 0, pressure: 0, tangentialPressure: 0, tiltX: 0, tiltY: 0, twist: 0, pointerType: 0, isPrimary: 0 }), Xs = Re(Rd), Id = J({}, jr, { touches: 0, targetTouches: 0, changedTouches: 0, altKey: 0, metaKey: 0, ctrlKey: 0, shiftKey: 0, getModifierState: Ji }), Od = Re(Id), Dd = J({}, Pn, { propertyName: 0, elapsedTime: 0, pseudoElement: 0 }), Wd = Re(Dd), Ad = J({}, Ml, {
  deltaX: function(e) {
    return "deltaX" in e ? e.deltaX : "wheelDeltaX" in e ? -e.wheelDeltaX : 0;
  },
  deltaY: function(e) {
    return "deltaY" in e ? e.deltaY : "wheelDeltaY" in e ? -e.wheelDeltaY : "wheelDelta" in e ? -e.wheelDelta : 0;
  },
  deltaZ: 0,
  deltaMode: 0
}), Ud = Re(Ad), $d = [9, 13, 27, 32], Zi = ut && "CompositionEvent" in window, Xn = null;
ut && "documentMode" in document && (Xn = document.documentMode);
var Hd = ut && "TextEvent" in window && !Xn, Ha = ut && (!Zi || Xn && 8 < Xn && 11 >= Xn), Ys = " ", Gs = !1;
function Ba(e, t) {
  switch (e) {
    case "keyup":
      return $d.indexOf(t.keyCode) !== -1;
    case "keydown":
      return t.keyCode !== 229;
    case "keypress":
    case "mousedown":
    case "focusout":
      return !0;
    default:
      return !1;
  }
}
function Va(e) {
  return e = e.detail, typeof e == "object" && "data" in e ? e.data : null;
}
var rn = !1;
function Bd(e, t) {
  switch (e) {
    case "compositionend":
      return Va(t);
    case "keypress":
      return t.which !== 32 ? null : (Gs = !0, Ys);
    case "textInput":
      return e = t.data, e === Ys && Gs ? null : e;
    default:
      return null;
  }
}
function Vd(e, t) {
  if (rn) return e === "compositionend" || !Zi && Ba(e, t) ? (e = $a(), Xr = Yi = xt = null, rn = !1, e) : null;
  switch (e) {
    case "paste":
      return null;
    case "keypress":
      if (!(t.ctrlKey || t.altKey || t.metaKey) || t.ctrlKey && t.altKey) {
        if (t.char && 1 < t.char.length) return t.char;
        if (t.which) return String.fromCharCode(t.which);
      }
      return null;
    case "compositionend":
      return Ha && t.locale !== "ko" ? null : t.data;
    default:
      return null;
  }
}
var Qd = { color: !0, date: !0, datetime: !0, "datetime-local": !0, email: !0, month: !0, number: !0, password: !0, range: !0, search: !0, tel: !0, text: !0, time: !0, url: !0, week: !0 };
function Js(e) {
  var t = e && e.nodeName && e.nodeName.toLowerCase();
  return t === "input" ? !!Qd[e.type] : t === "textarea";
}
function Qa(e, t, n, r) {
  ja(r), t = al(t, "onChange"), 0 < t.length && (n = new Gi("onChange", "change", null, n, r), e.push({ event: n, listeners: t }));
}
var Yn = null, ir = null;
function Kd(e) {
  nc(e, 0);
}
function Ll(e) {
  var t = sn(e);
  if (ha(t)) return e;
}
function Xd(e, t) {
  if (e === "change") return t;
}
var Ka = !1;
if (ut) {
  var io;
  if (ut) {
    var so = "oninput" in document;
    if (!so) {
      var Zs = document.createElement("div");
      Zs.setAttribute("oninput", "return;"), so = typeof Zs.oninput == "function";
    }
    io = so;
  } else io = !1;
  Ka = io && (!document.documentMode || 9 < document.documentMode);
}
function qs() {
  Yn && (Yn.detachEvent("onpropertychange", Xa), ir = Yn = null);
}
function Xa(e) {
  if (e.propertyName === "value" && Ll(ir)) {
    var t = [];
    Qa(t, ir, e, Bi(e)), Ea(Kd, t);
  }
}
function Yd(e, t, n) {
  e === "focusin" ? (qs(), Yn = t, ir = n, Yn.attachEvent("onpropertychange", Xa)) : e === "focusout" && qs();
}
function Gd(e) {
  if (e === "selectionchange" || e === "keyup" || e === "keydown") return Ll(ir);
}
function Jd(e, t) {
  if (e === "click") return Ll(t);
}
function Zd(e, t) {
  if (e === "input" || e === "change") return Ll(t);
}
function qd(e, t) {
  return e === t && (e !== 0 || 1 / e === 1 / t) || e !== e && t !== t;
}
var Ye = typeof Object.is == "function" ? Object.is : qd;
function sr(e, t) {
  if (Ye(e, t)) return !0;
  if (typeof e != "object" || e === null || typeof t != "object" || t === null) return !1;
  var n = Object.keys(e), r = Object.keys(t);
  if (n.length !== r.length) return !1;
  for (r = 0; r < n.length; r++) {
    var l = n[r];
    if (!Po.call(t, l) || !Ye(e[l], t[l])) return !1;
  }
  return !0;
}
function bs(e) {
  for (; e && e.firstChild; ) e = e.firstChild;
  return e;
}
function eu(e, t) {
  var n = bs(e);
  e = 0;
  for (var r; n; ) {
    if (n.nodeType === 3) {
      if (r = e + n.textContent.length, e <= t && r >= t) return { node: n, offset: t - e };
      e = r;
    }
    e: {
      for (; n; ) {
        if (n.nextSibling) {
          n = n.nextSibling;
          break e;
        }
        n = n.parentNode;
      }
      n = void 0;
    }
    n = bs(n);
  }
}
function Ya(e, t) {
  return e && t ? e === t ? !0 : e && e.nodeType === 3 ? !1 : t && t.nodeType === 3 ? Ya(e, t.parentNode) : "contains" in e ? e.contains(t) : e.compareDocumentPosition ? !!(e.compareDocumentPosition(t) & 16) : !1 : !1;
}
function Ga() {
  for (var e = window, t = nl(); t instanceof e.HTMLIFrameElement; ) {
    try {
      var n = typeof t.contentWindow.location.href == "string";
    } catch {
      n = !1;
    }
    if (n) e = t.contentWindow;
    else break;
    t = nl(e.document);
  }
  return t;
}
function qi(e) {
  var t = e && e.nodeName && e.nodeName.toLowerCase();
  return t && (t === "input" && (e.type === "text" || e.type === "search" || e.type === "tel" || e.type === "url" || e.type === "password") || t === "textarea" || e.contentEditable === "true");
}
function bd(e) {
  var t = Ga(), n = e.focusedElem, r = e.selectionRange;
  if (t !== n && n && n.ownerDocument && Ya(n.ownerDocument.documentElement, n)) {
    if (r !== null && qi(n)) {
      if (t = r.start, e = r.end, e === void 0 && (e = t), "selectionStart" in n) n.selectionStart = t, n.selectionEnd = Math.min(e, n.value.length);
      else if (e = (t = n.ownerDocument || document) && t.defaultView || window, e.getSelection) {
        e = e.getSelection();
        var l = n.textContent.length, o = Math.min(r.start, l);
        r = r.end === void 0 ? o : Math.min(r.end, l), !e.extend && o > r && (l = r, r = o, o = l), l = eu(n, o);
        var i = eu(
          n,
          r
        );
        l && i && (e.rangeCount !== 1 || e.anchorNode !== l.node || e.anchorOffset !== l.offset || e.focusNode !== i.node || e.focusOffset !== i.offset) && (t = t.createRange(), t.setStart(l.node, l.offset), e.removeAllRanges(), o > r ? (e.addRange(t), e.extend(i.node, i.offset)) : (t.setEnd(i.node, i.offset), e.addRange(t)));
      }
    }
    for (t = [], e = n; e = e.parentNode; ) e.nodeType === 1 && t.push({ element: e, left: e.scrollLeft, top: e.scrollTop });
    for (typeof n.focus == "function" && n.focus(), n = 0; n < t.length; n++) e = t[n], e.element.scrollLeft = e.left, e.element.scrollTop = e.top;
  }
}
var ep = ut && "documentMode" in document && 11 >= document.documentMode, ln = null, Go = null, Gn = null, Jo = !1;
function tu(e, t, n) {
  var r = n.window === n ? n.document : n.nodeType === 9 ? n : n.ownerDocument;
  Jo || ln == null || ln !== nl(r) || (r = ln, "selectionStart" in r && qi(r) ? r = { start: r.selectionStart, end: r.selectionEnd } : (r = (r.ownerDocument && r.ownerDocument.defaultView || window).getSelection(), r = { anchorNode: r.anchorNode, anchorOffset: r.anchorOffset, focusNode: r.focusNode, focusOffset: r.focusOffset }), Gn && sr(Gn, r) || (Gn = r, r = al(Go, "onSelect"), 0 < r.length && (t = new Gi("onSelect", "select", null, t, n), e.push({ event: t, listeners: r }), t.target = ln)));
}
function Rr(e, t) {
  var n = {};
  return n[e.toLowerCase()] = t.toLowerCase(), n["Webkit" + e] = "webkit" + t, n["Moz" + e] = "moz" + t, n;
}
var on = { animationend: Rr("Animation", "AnimationEnd"), animationiteration: Rr("Animation", "AnimationIteration"), animationstart: Rr("Animation", "AnimationStart"), transitionend: Rr("Transition", "TransitionEnd") }, uo = {}, Ja = {};
ut && (Ja = document.createElement("div").style, "AnimationEvent" in window || (delete on.animationend.animation, delete on.animationiteration.animation, delete on.animationstart.animation), "TransitionEvent" in window || delete on.transitionend.transition);
function Rl(e) {
  if (uo[e]) return uo[e];
  if (!on[e]) return e;
  var t = on[e], n;
  for (n in t) if (t.hasOwnProperty(n) && n in Ja) return uo[e] = t[n];
  return e;
}
var Za = Rl("animationend"), qa = Rl("animationiteration"), ba = Rl("animationstart"), ec = Rl("transitionend"), tc = /* @__PURE__ */ new Map(), nu = "abort auxClick cancel canPlay canPlayThrough click close contextMenu copy cut drag dragEnd dragEnter dragExit dragLeave dragOver dragStart drop durationChange emptied encrypted ended error gotPointerCapture input invalid keyDown keyPress keyUp load loadedData loadedMetadata loadStart lostPointerCapture mouseDown mouseMove mouseOut mouseOver mouseUp paste pause play playing pointerCancel pointerDown pointerMove pointerOut pointerOver pointerUp progress rateChange reset resize seeked seeking stalled submit suspend timeUpdate touchCancel touchEnd touchStart volumeChange scroll toggle touchMove waiting wheel".split(" ");
function zt(e, t) {
  tc.set(e, t), Jt(t, [e]);
}
for (var ao = 0; ao < nu.length; ao++) {
  var co = nu[ao], tp = co.toLowerCase(), np = co[0].toUpperCase() + co.slice(1);
  zt(tp, "on" + np);
}
zt(Za, "onAnimationEnd");
zt(qa, "onAnimationIteration");
zt(ba, "onAnimationStart");
zt("dblclick", "onDoubleClick");
zt("focusin", "onFocus");
zt("focusout", "onBlur");
zt(ec, "onTransitionEnd");
wn("onMouseEnter", ["mouseout", "mouseover"]);
wn("onMouseLeave", ["mouseout", "mouseover"]);
wn("onPointerEnter", ["pointerout", "pointerover"]);
wn("onPointerLeave", ["pointerout", "pointerover"]);
Jt("onChange", "change click focusin focusout input keydown keyup selectionchange".split(" "));
Jt("onSelect", "focusout contextmenu dragend focusin keydown keyup mousedown mouseup selectionchange".split(" "));
Jt("onBeforeInput", ["compositionend", "keypress", "textInput", "paste"]);
Jt("onCompositionEnd", "compositionend focusout keydown keypress keyup mousedown".split(" "));
Jt("onCompositionStart", "compositionstart focusout keydown keypress keyup mousedown".split(" "));
Jt("onCompositionUpdate", "compositionupdate focusout keydown keypress keyup mousedown".split(" "));
var Vn = "abort canplay canplaythrough durationchange emptied encrypted ended error loadeddata loadedmetadata loadstart pause play playing progress ratechange resize seeked seeking stalled suspend timeupdate volumechange waiting".split(" "), rp = new Set("cancel close invalid load scroll toggle".split(" ").concat(Vn));
function ru(e, t, n) {
  var r = e.type || "unknown-event";
  e.currentTarget = n, td(r, t, void 0, e), e.currentTarget = null;
}
function nc(e, t) {
  t = (t & 4) !== 0;
  for (var n = 0; n < e.length; n++) {
    var r = e[n], l = r.event;
    r = r.listeners;
    e: {
      var o = void 0;
      if (t) for (var i = r.length - 1; 0 <= i; i--) {
        var s = r[i], u = s.instance, a = s.currentTarget;
        if (s = s.listener, u !== o && l.isPropagationStopped()) break e;
        ru(l, s, a), o = u;
      }
      else for (i = 0; i < r.length; i++) {
        if (s = r[i], u = s.instance, a = s.currentTarget, s = s.listener, u !== o && l.isPropagationStopped()) break e;
        ru(l, s, a), o = u;
      }
    }
  }
  if (ll) throw e = Qo, ll = !1, Qo = null, e;
}
function Q(e, t) {
  var n = t[ti];
  n === void 0 && (n = t[ti] = /* @__PURE__ */ new Set());
  var r = e + "__bubble";
  n.has(r) || (rc(t, e, 2, !1), n.add(r));
}
function fo(e, t, n) {
  var r = 0;
  t && (r |= 4), rc(n, e, r, t);
}
var Ir = "_reactListening" + Math.random().toString(36).slice(2);
function ur(e) {
  if (!e[Ir]) {
    e[Ir] = !0, ca.forEach(function(n) {
      n !== "selectionchange" && (rp.has(n) || fo(n, !1, e), fo(n, !0, e));
    });
    var t = e.nodeType === 9 ? e : e.ownerDocument;
    t === null || t[Ir] || (t[Ir] = !0, fo("selectionchange", !1, t));
  }
}
function rc(e, t, n, r) {
  switch (Ua(t)) {
    case 1:
      var l = gd;
      break;
    case 4:
      l = yd;
      break;
    default:
      l = Xi;
  }
  n = l.bind(null, t, n, e), l = void 0, !Vo || t !== "touchstart" && t !== "touchmove" && t !== "wheel" || (l = !0), r ? l !== void 0 ? e.addEventListener(t, n, { capture: !0, passive: l }) : e.addEventListener(t, n, !0) : l !== void 0 ? e.addEventListener(t, n, { passive: l }) : e.addEventListener(t, n, !1);
}
function po(e, t, n, r, l) {
  var o = r;
  if (!(t & 1) && !(t & 2) && r !== null) e: for (; ; ) {
    if (r === null) return;
    var i = r.tag;
    if (i === 3 || i === 4) {
      var s = r.stateNode.containerInfo;
      if (s === l || s.nodeType === 8 && s.parentNode === l) break;
      if (i === 4) for (i = r.return; i !== null; ) {
        var u = i.tag;
        if ((u === 3 || u === 4) && (u = i.stateNode.containerInfo, u === l || u.nodeType === 8 && u.parentNode === l)) return;
        i = i.return;
      }
      for (; s !== null; ) {
        if (i = Wt(s), i === null) return;
        if (u = i.tag, u === 5 || u === 6) {
          r = o = i;
          continue e;
        }
        s = s.parentNode;
      }
    }
    r = r.return;
  }
  Ea(function() {
    var a = o, p = Bi(n), h = [];
    e: {
      var m = tc.get(e);
      if (m !== void 0) {
        var x = Gi, _ = e;
        switch (e) {
          case "keypress":
            if (Yr(n) === 0) break e;
          case "keydown":
          case "keyup":
            x = Ld;
            break;
          case "focusin":
            _ = "focus", x = oo;
            break;
          case "focusout":
            _ = "blur", x = oo;
            break;
          case "beforeblur":
          case "afterblur":
            x = oo;
            break;
          case "click":
            if (n.button === 2) break e;
          case "auxclick":
          case "dblclick":
          case "mousedown":
          case "mousemove":
          case "mouseup":
          case "mouseout":
          case "mouseover":
          case "contextmenu":
            x = Qs;
            break;
          case "drag":
          case "dragend":
          case "dragenter":
          case "dragexit":
          case "dragleave":
          case "dragover":
          case "dragstart":
          case "drop":
            x = _d;
            break;
          case "touchcancel":
          case "touchend":
          case "touchmove":
          case "touchstart":
            x = Od;
            break;
          case Za:
          case qa:
          case ba:
            x = Sd;
            break;
          case ec:
            x = Wd;
            break;
          case "scroll":
            x = xd;
            break;
          case "wheel":
            x = Ud;
            break;
          case "copy":
          case "cut":
          case "paste":
            x = Ed;
            break;
          case "gotpointercapture":
          case "lostpointercapture":
          case "pointercancel":
          case "pointerdown":
          case "pointermove":
          case "pointerout":
          case "pointerover":
          case "pointerup":
            x = Xs;
        }
        var k = (t & 4) !== 0, I = !k && e === "scroll", f = k ? m !== null ? m + "Capture" : null : m;
        k = [];
        for (var c = a, d; c !== null; ) {
          d = c;
          var y = d.stateNode;
          if (d.tag === 5 && y !== null && (d = y, f !== null && (y = nr(c, f), y != null && k.push(ar(c, y, d)))), I) break;
          c = c.return;
        }
        0 < k.length && (m = new x(m, _, null, n, p), h.push({ event: m, listeners: k }));
      }
    }
    if (!(t & 7)) {
      e: {
        if (m = e === "mouseover" || e === "pointerover", x = e === "mouseout" || e === "pointerout", m && n !== Ho && (_ = n.relatedTarget || n.fromElement) && (Wt(_) || _[at])) break e;
        if ((x || m) && (m = p.window === p ? p : (m = p.ownerDocument) ? m.defaultView || m.parentWindow : window, x ? (_ = n.relatedTarget || n.toElement, x = a, _ = _ ? Wt(_) : null, _ !== null && (I = Zt(_), _ !== I || _.tag !== 5 && _.tag !== 6) && (_ = null)) : (x = null, _ = a), x !== _)) {
          if (k = Qs, y = "onMouseLeave", f = "onMouseEnter", c = "mouse", (e === "pointerout" || e === "pointerover") && (k = Xs, y = "onPointerLeave", f = "onPointerEnter", c = "pointer"), I = x == null ? m : sn(x), d = _ == null ? m : sn(_), m = new k(y, c + "leave", x, n, p), m.target = I, m.relatedTarget = d, y = null, Wt(p) === a && (k = new k(f, c + "enter", _, n, p), k.target = d, k.relatedTarget = I, y = k), I = y, x && _) t: {
            for (k = x, f = _, c = 0, d = k; d; d = bt(d)) c++;
            for (d = 0, y = f; y; y = bt(y)) d++;
            for (; 0 < c - d; ) k = bt(k), c--;
            for (; 0 < d - c; ) f = bt(f), d--;
            for (; c--; ) {
              if (k === f || f !== null && k === f.alternate) break t;
              k = bt(k), f = bt(f);
            }
            k = null;
          }
          else k = null;
          x !== null && lu(h, m, x, k, !1), _ !== null && I !== null && lu(h, I, _, k, !0);
        }
      }
      e: {
        if (m = a ? sn(a) : window, x = m.nodeName && m.nodeName.toLowerCase(), x === "select" || x === "input" && m.type === "file") var S = Xd;
        else if (Js(m)) if (Ka) S = Zd;
        else {
          S = Gd;
          var w = Yd;
        }
        else (x = m.nodeName) && x.toLowerCase() === "input" && (m.type === "checkbox" || m.type === "radio") && (S = Jd);
        if (S && (S = S(e, a))) {
          Qa(h, S, n, p);
          break e;
        }
        w && w(e, m, a), e === "focusout" && (w = m._wrapperState) && w.controlled && m.type === "number" && Do(m, "number", m.value);
      }
      switch (w = a ? sn(a) : window, e) {
        case "focusin":
          (Js(w) || w.contentEditable === "true") && (ln = w, Go = a, Gn = null);
          break;
        case "focusout":
          Gn = Go = ln = null;
          break;
        case "mousedown":
          Jo = !0;
          break;
        case "contextmenu":
        case "mouseup":
        case "dragend":
          Jo = !1, tu(h, n, p);
          break;
        case "selectionchange":
          if (ep) break;
        case "keydown":
        case "keyup":
          tu(h, n, p);
      }
      var g;
      if (Zi) e: {
        switch (e) {
          case "compositionstart":
            var C = "onCompositionStart";
            break e;
          case "compositionend":
            C = "onCompositionEnd";
            break e;
          case "compositionupdate":
            C = "onCompositionUpdate";
            break e;
        }
        C = void 0;
      }
      else rn ? Ba(e, n) && (C = "onCompositionEnd") : e === "keydown" && n.keyCode === 229 && (C = "onCompositionStart");
      C && (Ha && n.locale !== "ko" && (rn || C !== "onCompositionStart" ? C === "onCompositionEnd" && rn && (g = $a()) : (xt = p, Yi = "value" in xt ? xt.value : xt.textContent, rn = !0)), w = al(a, C), 0 < w.length && (C = new Ks(C, e, null, n, p), h.push({ event: C, listeners: w }), g ? C.data = g : (g = Va(n), g !== null && (C.data = g)))), (g = Hd ? Bd(e, n) : Vd(e, n)) && (a = al(a, "onBeforeInput"), 0 < a.length && (p = new Ks("onBeforeInput", "beforeinput", null, n, p), h.push({ event: p, listeners: a }), p.data = g));
    }
    nc(h, t);
  });
}
function ar(e, t, n) {
  return { instance: e, listener: t, currentTarget: n };
}
function al(e, t) {
  for (var n = t + "Capture", r = []; e !== null; ) {
    var l = e, o = l.stateNode;
    l.tag === 5 && o !== null && (l = o, o = nr(e, n), o != null && r.unshift(ar(e, o, l)), o = nr(e, t), o != null && r.push(ar(e, o, l))), e = e.return;
  }
  return r;
}
function bt(e) {
  if (e === null) return null;
  do
    e = e.return;
  while (e && e.tag !== 5);
  return e || null;
}
function lu(e, t, n, r, l) {
  for (var o = t._reactName, i = []; n !== null && n !== r; ) {
    var s = n, u = s.alternate, a = s.stateNode;
    if (u !== null && u === r) break;
    s.tag === 5 && a !== null && (s = a, l ? (u = nr(n, o), u != null && i.unshift(ar(n, u, s))) : l || (u = nr(n, o), u != null && i.push(ar(n, u, s)))), n = n.return;
  }
  i.length !== 0 && e.push({ event: t, listeners: i });
}
var lp = /\r\n?/g, op = /\u0000|\uFFFD/g;
function ou(e) {
  return (typeof e == "string" ? e : "" + e).replace(lp, `
`).replace(op, "");
}
function Or(e, t, n) {
  if (t = ou(t), ou(e) !== t && n) throw Error(j(425));
}
function cl() {
}
var Zo = null, qo = null;
function bo(e, t) {
  return e === "textarea" || e === "noscript" || typeof t.children == "string" || typeof t.children == "number" || typeof t.dangerouslySetInnerHTML == "object" && t.dangerouslySetInnerHTML !== null && t.dangerouslySetInnerHTML.__html != null;
}
var ei = typeof setTimeout == "function" ? setTimeout : void 0, ip = typeof clearTimeout == "function" ? clearTimeout : void 0, iu = typeof Promise == "function" ? Promise : void 0, sp = typeof queueMicrotask == "function" ? queueMicrotask : typeof iu < "u" ? function(e) {
  return iu.resolve(null).then(e).catch(up);
} : ei;
function up(e) {
  setTimeout(function() {
    throw e;
  });
}
function mo(e, t) {
  var n = t, r = 0;
  do {
    var l = n.nextSibling;
    if (e.removeChild(n), l && l.nodeType === 8) if (n = l.data, n === "/$") {
      if (r === 0) {
        e.removeChild(l), or(t);
        return;
      }
      r--;
    } else n !== "$" && n !== "$?" && n !== "$!" || r++;
    n = l;
  } while (n);
  or(t);
}
function St(e) {
  for (; e != null; e = e.nextSibling) {
    var t = e.nodeType;
    if (t === 1 || t === 3) break;
    if (t === 8) {
      if (t = e.data, t === "$" || t === "$!" || t === "$?") break;
      if (t === "/$") return null;
    }
  }
  return e;
}
function su(e) {
  e = e.previousSibling;
  for (var t = 0; e; ) {
    if (e.nodeType === 8) {
      var n = e.data;
      if (n === "$" || n === "$!" || n === "$?") {
        if (t === 0) return e;
        t--;
      } else n === "/$" && t++;
    }
    e = e.previousSibling;
  }
  return null;
}
var zn = Math.random().toString(36).slice(2), qe = "__reactFiber$" + zn, cr = "__reactProps$" + zn, at = "__reactContainer$" + zn, ti = "__reactEvents$" + zn, ap = "__reactListeners$" + zn, cp = "__reactHandles$" + zn;
function Wt(e) {
  var t = e[qe];
  if (t) return t;
  for (var n = e.parentNode; n; ) {
    if (t = n[at] || n[qe]) {
      if (n = t.alternate, t.child !== null || n !== null && n.child !== null) for (e = su(e); e !== null; ) {
        if (n = e[qe]) return n;
        e = su(e);
      }
      return t;
    }
    e = n, n = e.parentNode;
  }
  return null;
}
function kr(e) {
  return e = e[qe] || e[at], !e || e.tag !== 5 && e.tag !== 6 && e.tag !== 13 && e.tag !== 3 ? null : e;
}
function sn(e) {
  if (e.tag === 5 || e.tag === 6) return e.stateNode;
  throw Error(j(33));
}
function Il(e) {
  return e[cr] || null;
}
var ni = [], un = -1;
function Mt(e) {
  return { current: e };
}
function K(e) {
  0 > un || (e.current = ni[un], ni[un] = null, un--);
}
function B(e, t) {
  un++, ni[un] = e.current, e.current = t;
}
var Pt = {}, ve = Mt(Pt), Se = Mt(!1), Bt = Pt;
function _n(e, t) {
  var n = e.type.contextTypes;
  if (!n) return Pt;
  var r = e.stateNode;
  if (r && r.__reactInternalMemoizedUnmaskedChildContext === t) return r.__reactInternalMemoizedMaskedChildContext;
  var l = {}, o;
  for (o in n) l[o] = t[o];
  return r && (e = e.stateNode, e.__reactInternalMemoizedUnmaskedChildContext = t, e.__reactInternalMemoizedMaskedChildContext = l), l;
}
function Ce(e) {
  return e = e.childContextTypes, e != null;
}
function fl() {
  K(Se), K(ve);
}
function uu(e, t, n) {
  if (ve.current !== Pt) throw Error(j(168));
  B(ve, t), B(Se, n);
}
function lc(e, t, n) {
  var r = e.stateNode;
  if (t = t.childContextTypes, typeof r.getChildContext != "function") return n;
  r = r.getChildContext();
  for (var l in r) if (!(l in t)) throw Error(j(108, Yf(e) || "Unknown", l));
  return J({}, n, r);
}
function dl(e) {
  return e = (e = e.stateNode) && e.__reactInternalMemoizedMergedChildContext || Pt, Bt = ve.current, B(ve, e), B(Se, Se.current), !0;
}
function au(e, t, n) {
  var r = e.stateNode;
  if (!r) throw Error(j(169));
  n ? (e = lc(e, t, Bt), r.__reactInternalMemoizedMergedChildContext = e, K(Se), K(ve), B(ve, e)) : K(Se), B(Se, n);
}
var lt = null, Ol = !1, ho = !1;
function oc(e) {
  lt === null ? lt = [e] : lt.push(e);
}
function fp(e) {
  Ol = !0, oc(e);
}
function Lt() {
  if (!ho && lt !== null) {
    ho = !0;
    var e = 0, t = A;
    try {
      var n = lt;
      for (A = 1; e < n.length; e++) {
        var r = n[e];
        do
          r = r(!0);
        while (r !== null);
      }
      lt = null, Ol = !1;
    } catch (l) {
      throw lt !== null && (lt = lt.slice(e + 1)), Pa(Vi, Lt), l;
    } finally {
      A = t, ho = !1;
    }
  }
  return null;
}
var an = [], cn = 0, pl = null, ml = 0, Ie = [], Oe = 0, Vt = null, ot = 1, it = "";
function It(e, t) {
  an[cn++] = ml, an[cn++] = pl, pl = e, ml = t;
}
function ic(e, t, n) {
  Ie[Oe++] = ot, Ie[Oe++] = it, Ie[Oe++] = Vt, Vt = e;
  var r = ot;
  e = it;
  var l = 32 - Ke(r) - 1;
  r &= ~(1 << l), n += 1;
  var o = 32 - Ke(t) + l;
  if (30 < o) {
    var i = l - l % 5;
    o = (r & (1 << i) - 1).toString(32), r >>= i, l -= i, ot = 1 << 32 - Ke(t) + l | n << l | r, it = o + e;
  } else ot = 1 << o | n << l | r, it = e;
}
function bi(e) {
  e.return !== null && (It(e, 1), ic(e, 1, 0));
}
function es(e) {
  for (; e === pl; ) pl = an[--cn], an[cn] = null, ml = an[--cn], an[cn] = null;
  for (; e === Vt; ) Vt = Ie[--Oe], Ie[Oe] = null, it = Ie[--Oe], Ie[Oe] = null, ot = Ie[--Oe], Ie[Oe] = null;
}
var Pe = null, Te = null, X = !1, Ve = null;
function sc(e, t) {
  var n = De(5, null, null, 0);
  n.elementType = "DELETED", n.stateNode = t, n.return = e, t = e.deletions, t === null ? (e.deletions = [n], e.flags |= 16) : t.push(n);
}
function cu(e, t) {
  switch (e.tag) {
    case 5:
      var n = e.type;
      return t = t.nodeType !== 1 || n.toLowerCase() !== t.nodeName.toLowerCase() ? null : t, t !== null ? (e.stateNode = t, Pe = e, Te = St(t.firstChild), !0) : !1;
    case 6:
      return t = e.pendingProps === "" || t.nodeType !== 3 ? null : t, t !== null ? (e.stateNode = t, Pe = e, Te = null, !0) : !1;
    case 13:
      return t = t.nodeType !== 8 ? null : t, t !== null ? (n = Vt !== null ? { id: ot, overflow: it } : null, e.memoizedState = { dehydrated: t, treeContext: n, retryLane: 1073741824 }, n = De(18, null, null, 0), n.stateNode = t, n.return = e, e.child = n, Pe = e, Te = null, !0) : !1;
    default:
      return !1;
  }
}
function ri(e) {
  return (e.mode & 1) !== 0 && (e.flags & 128) === 0;
}
function li(e) {
  if (X) {
    var t = Te;
    if (t) {
      var n = t;
      if (!cu(e, t)) {
        if (ri(e)) throw Error(j(418));
        t = St(n.nextSibling);
        var r = Pe;
        t && cu(e, t) ? sc(r, n) : (e.flags = e.flags & -4097 | 2, X = !1, Pe = e);
      }
    } else {
      if (ri(e)) throw Error(j(418));
      e.flags = e.flags & -4097 | 2, X = !1, Pe = e;
    }
  }
}
function fu(e) {
  for (e = e.return; e !== null && e.tag !== 5 && e.tag !== 3 && e.tag !== 13; ) e = e.return;
  Pe = e;
}
function Dr(e) {
  if (e !== Pe) return !1;
  if (!X) return fu(e), X = !0, !1;
  var t;
  if ((t = e.tag !== 3) && !(t = e.tag !== 5) && (t = e.type, t = t !== "head" && t !== "body" && !bo(e.type, e.memoizedProps)), t && (t = Te)) {
    if (ri(e)) throw uc(), Error(j(418));
    for (; t; ) sc(e, t), t = St(t.nextSibling);
  }
  if (fu(e), e.tag === 13) {
    if (e = e.memoizedState, e = e !== null ? e.dehydrated : null, !e) throw Error(j(317));
    e: {
      for (e = e.nextSibling, t = 0; e; ) {
        if (e.nodeType === 8) {
          var n = e.data;
          if (n === "/$") {
            if (t === 0) {
              Te = St(e.nextSibling);
              break e;
            }
            t--;
          } else n !== "$" && n !== "$!" && n !== "$?" || t++;
        }
        e = e.nextSibling;
      }
      Te = null;
    }
  } else Te = Pe ? St(e.stateNode.nextSibling) : null;
  return !0;
}
function uc() {
  for (var e = Te; e; ) e = St(e.nextSibling);
}
function jn() {
  Te = Pe = null, X = !1;
}
function ts(e) {
  Ve === null ? Ve = [e] : Ve.push(e);
}
var dp = dt.ReactCurrentBatchConfig;
function Wn(e, t, n) {
  if (e = n.ref, e !== null && typeof e != "function" && typeof e != "object") {
    if (n._owner) {
      if (n = n._owner, n) {
        if (n.tag !== 1) throw Error(j(309));
        var r = n.stateNode;
      }
      if (!r) throw Error(j(147, e));
      var l = r, o = "" + e;
      return t !== null && t.ref !== null && typeof t.ref == "function" && t.ref._stringRef === o ? t.ref : (t = function(i) {
        var s = l.refs;
        i === null ? delete s[o] : s[o] = i;
      }, t._stringRef = o, t);
    }
    if (typeof e != "string") throw Error(j(284));
    if (!n._owner) throw Error(j(290, e));
  }
  return e;
}
function Wr(e, t) {
  throw e = Object.prototype.toString.call(t), Error(j(31, e === "[object Object]" ? "object with keys {" + Object.keys(t).join(", ") + "}" : e));
}
function du(e) {
  var t = e._init;
  return t(e._payload);
}
function ac(e) {
  function t(f, c) {
    if (e) {
      var d = f.deletions;
      d === null ? (f.deletions = [c], f.flags |= 16) : d.push(c);
    }
  }
  function n(f, c) {
    if (!e) return null;
    for (; c !== null; ) t(f, c), c = c.sibling;
    return null;
  }
  function r(f, c) {
    for (f = /* @__PURE__ */ new Map(); c !== null; ) c.key !== null ? f.set(c.key, c) : f.set(c.index, c), c = c.sibling;
    return f;
  }
  function l(f, c) {
    return f = Nt(f, c), f.index = 0, f.sibling = null, f;
  }
  function o(f, c, d) {
    return f.index = d, e ? (d = f.alternate, d !== null ? (d = d.index, d < c ? (f.flags |= 2, c) : d) : (f.flags |= 2, c)) : (f.flags |= 1048576, c);
  }
  function i(f) {
    return e && f.alternate === null && (f.flags |= 2), f;
  }
  function s(f, c, d, y) {
    return c === null || c.tag !== 6 ? (c = jo(d, f.mode, y), c.return = f, c) : (c = l(c, d), c.return = f, c);
  }
  function u(f, c, d, y) {
    var S = d.type;
    return S === nn ? p(f, c, d.props.children, y, d.key) : c !== null && (c.elementType === S || typeof S == "object" && S !== null && S.$$typeof === ht && du(S) === c.type) ? (y = l(c, d.props), y.ref = Wn(f, c, d), y.return = f, y) : (y = tl(d.type, d.key, d.props, null, f.mode, y), y.ref = Wn(f, c, d), y.return = f, y);
  }
  function a(f, c, d, y) {
    return c === null || c.tag !== 4 || c.stateNode.containerInfo !== d.containerInfo || c.stateNode.implementation !== d.implementation ? (c = ko(d, f.mode, y), c.return = f, c) : (c = l(c, d.children || []), c.return = f, c);
  }
  function p(f, c, d, y, S) {
    return c === null || c.tag !== 7 ? (c = Ht(d, f.mode, y, S), c.return = f, c) : (c = l(c, d), c.return = f, c);
  }
  function h(f, c, d) {
    if (typeof c == "string" && c !== "" || typeof c == "number") return c = jo("" + c, f.mode, d), c.return = f, c;
    if (typeof c == "object" && c !== null) {
      switch (c.$$typeof) {
        case Fr:
          return d = tl(c.type, c.key, c.props, null, f.mode, d), d.ref = Wn(f, null, c), d.return = f, d;
        case tn:
          return c = ko(c, f.mode, d), c.return = f, c;
        case ht:
          var y = c._init;
          return h(f, y(c._payload), d);
      }
      if (Hn(c) || Ln(c)) return c = Ht(c, f.mode, d, null), c.return = f, c;
      Wr(f, c);
    }
    return null;
  }
  function m(f, c, d, y) {
    var S = c !== null ? c.key : null;
    if (typeof d == "string" && d !== "" || typeof d == "number") return S !== null ? null : s(f, c, "" + d, y);
    if (typeof d == "object" && d !== null) {
      switch (d.$$typeof) {
        case Fr:
          return d.key === S ? u(f, c, d, y) : null;
        case tn:
          return d.key === S ? a(f, c, d, y) : null;
        case ht:
          return S = d._init, m(
            f,
            c,
            S(d._payload),
            y
          );
      }
      if (Hn(d) || Ln(d)) return S !== null ? null : p(f, c, d, y, null);
      Wr(f, d);
    }
    return null;
  }
  function x(f, c, d, y, S) {
    if (typeof y == "string" && y !== "" || typeof y == "number") return f = f.get(d) || null, s(c, f, "" + y, S);
    if (typeof y == "object" && y !== null) {
      switch (y.$$typeof) {
        case Fr:
          return f = f.get(y.key === null ? d : y.key) || null, u(c, f, y, S);
        case tn:
          return f = f.get(y.key === null ? d : y.key) || null, a(c, f, y, S);
        case ht:
          var w = y._init;
          return x(f, c, d, w(y._payload), S);
      }
      if (Hn(y) || Ln(y)) return f = f.get(d) || null, p(c, f, y, S, null);
      Wr(c, y);
    }
    return null;
  }
  function _(f, c, d, y) {
    for (var S = null, w = null, g = c, C = c = 0, z = null; g !== null && C < d.length; C++) {
      g.index > C ? (z = g, g = null) : z = g.sibling;
      var M = m(f, g, d[C], y);
      if (M === null) {
        g === null && (g = z);
        break;
      }
      e && g && M.alternate === null && t(f, g), c = o(M, c, C), w === null ? S = M : w.sibling = M, w = M, g = z;
    }
    if (C === d.length) return n(f, g), X && It(f, C), S;
    if (g === null) {
      for (; C < d.length; C++) g = h(f, d[C], y), g !== null && (c = o(g, c, C), w === null ? S = g : w.sibling = g, w = g);
      return X && It(f, C), S;
    }
    for (g = r(f, g); C < d.length; C++) z = x(g, f, C, d[C], y), z !== null && (e && z.alternate !== null && g.delete(z.key === null ? C : z.key), c = o(z, c, C), w === null ? S = z : w.sibling = z, w = z);
    return e && g.forEach(function(U) {
      return t(f, U);
    }), X && It(f, C), S;
  }
  function k(f, c, d, y) {
    var S = Ln(d);
    if (typeof S != "function") throw Error(j(150));
    if (d = S.call(d), d == null) throw Error(j(151));
    for (var w = S = null, g = c, C = c = 0, z = null, M = d.next(); g !== null && !M.done; C++, M = d.next()) {
      g.index > C ? (z = g, g = null) : z = g.sibling;
      var U = m(f, g, M.value, y);
      if (U === null) {
        g === null && (g = z);
        break;
      }
      e && g && U.alternate === null && t(f, g), c = o(U, c, C), w === null ? S = U : w.sibling = U, w = U, g = z;
    }
    if (M.done) return n(
      f,
      g
    ), X && It(f, C), S;
    if (g === null) {
      for (; !M.done; C++, M = d.next()) M = h(f, M.value, y), M !== null && (c = o(M, c, C), w === null ? S = M : w.sibling = M, w = M);
      return X && It(f, C), S;
    }
    for (g = r(f, g); !M.done; C++, M = d.next()) M = x(g, f, C, M.value, y), M !== null && (e && M.alternate !== null && g.delete(M.key === null ? C : M.key), c = o(M, c, C), w === null ? S = M : w.sibling = M, w = M);
    return e && g.forEach(function(ce) {
      return t(f, ce);
    }), X && It(f, C), S;
  }
  function I(f, c, d, y) {
    if (typeof d == "object" && d !== null && d.type === nn && d.key === null && (d = d.props.children), typeof d == "object" && d !== null) {
      switch (d.$$typeof) {
        case Fr:
          e: {
            for (var S = d.key, w = c; w !== null; ) {
              if (w.key === S) {
                if (S = d.type, S === nn) {
                  if (w.tag === 7) {
                    n(f, w.sibling), c = l(w, d.props.children), c.return = f, f = c;
                    break e;
                  }
                } else if (w.elementType === S || typeof S == "object" && S !== null && S.$$typeof === ht && du(S) === w.type) {
                  n(f, w.sibling), c = l(w, d.props), c.ref = Wn(f, w, d), c.return = f, f = c;
                  break e;
                }
                n(f, w);
                break;
              } else t(f, w);
              w = w.sibling;
            }
            d.type === nn ? (c = Ht(d.props.children, f.mode, y, d.key), c.return = f, f = c) : (y = tl(d.type, d.key, d.props, null, f.mode, y), y.ref = Wn(f, c, d), y.return = f, f = y);
          }
          return i(f);
        case tn:
          e: {
            for (w = d.key; c !== null; ) {
              if (c.key === w) if (c.tag === 4 && c.stateNode.containerInfo === d.containerInfo && c.stateNode.implementation === d.implementation) {
                n(f, c.sibling), c = l(c, d.children || []), c.return = f, f = c;
                break e;
              } else {
                n(f, c);
                break;
              }
              else t(f, c);
              c = c.sibling;
            }
            c = ko(d, f.mode, y), c.return = f, f = c;
          }
          return i(f);
        case ht:
          return w = d._init, I(f, c, w(d._payload), y);
      }
      if (Hn(d)) return _(f, c, d, y);
      if (Ln(d)) return k(f, c, d, y);
      Wr(f, d);
    }
    return typeof d == "string" && d !== "" || typeof d == "number" ? (d = "" + d, c !== null && c.tag === 6 ? (n(f, c.sibling), c = l(c, d), c.return = f, f = c) : (n(f, c), c = jo(d, f.mode, y), c.return = f, f = c), i(f)) : n(f, c);
  }
  return I;
}
var kn = ac(!0), cc = ac(!1), hl = Mt(null), vl = null, fn = null, ns = null;
function rs() {
  ns = fn = vl = null;
}
function ls(e) {
  var t = hl.current;
  K(hl), e._currentValue = t;
}
function oi(e, t, n) {
  for (; e !== null; ) {
    var r = e.alternate;
    if ((e.childLanes & t) !== t ? (e.childLanes |= t, r !== null && (r.childLanes |= t)) : r !== null && (r.childLanes & t) !== t && (r.childLanes |= t), e === n) break;
    e = e.return;
  }
}
function yn(e, t) {
  vl = e, ns = fn = null, e = e.dependencies, e !== null && e.firstContext !== null && (e.lanes & t && (ke = !0), e.firstContext = null);
}
function Ae(e) {
  var t = e._currentValue;
  if (ns !== e) if (e = { context: e, memoizedValue: t, next: null }, fn === null) {
    if (vl === null) throw Error(j(308));
    fn = e, vl.dependencies = { lanes: 0, firstContext: e };
  } else fn = fn.next = e;
  return t;
}
var At = null;
function os(e) {
  At === null ? At = [e] : At.push(e);
}
function fc(e, t, n, r) {
  var l = t.interleaved;
  return l === null ? (n.next = n, os(t)) : (n.next = l.next, l.next = n), t.interleaved = n, ct(e, r);
}
function ct(e, t) {
  e.lanes |= t;
  var n = e.alternate;
  for (n !== null && (n.lanes |= t), n = e, e = e.return; e !== null; ) e.childLanes |= t, n = e.alternate, n !== null && (n.childLanes |= t), n = e, e = e.return;
  return n.tag === 3 ? n.stateNode : null;
}
var vt = !1;
function is(e) {
  e.updateQueue = { baseState: e.memoizedState, firstBaseUpdate: null, lastBaseUpdate: null, shared: { pending: null, interleaved: null, lanes: 0 }, effects: null };
}
function dc(e, t) {
  e = e.updateQueue, t.updateQueue === e && (t.updateQueue = { baseState: e.baseState, firstBaseUpdate: e.firstBaseUpdate, lastBaseUpdate: e.lastBaseUpdate, shared: e.shared, effects: e.effects });
}
function st(e, t) {
  return { eventTime: e, lane: t, tag: 0, payload: null, callback: null, next: null };
}
function Ct(e, t, n) {
  var r = e.updateQueue;
  if (r === null) return null;
  if (r = r.shared, D & 2) {
    var l = r.pending;
    return l === null ? t.next = t : (t.next = l.next, l.next = t), r.pending = t, ct(e, n);
  }
  return l = r.interleaved, l === null ? (t.next = t, os(r)) : (t.next = l.next, l.next = t), r.interleaved = t, ct(e, n);
}
function Gr(e, t, n) {
  if (t = t.updateQueue, t !== null && (t = t.shared, (n & 4194240) !== 0)) {
    var r = t.lanes;
    r &= e.pendingLanes, n |= r, t.lanes = n, Qi(e, n);
  }
}
function pu(e, t) {
  var n = e.updateQueue, r = e.alternate;
  if (r !== null && (r = r.updateQueue, n === r)) {
    var l = null, o = null;
    if (n = n.firstBaseUpdate, n !== null) {
      do {
        var i = { eventTime: n.eventTime, lane: n.lane, tag: n.tag, payload: n.payload, callback: n.callback, next: null };
        o === null ? l = o = i : o = o.next = i, n = n.next;
      } while (n !== null);
      o === null ? l = o = t : o = o.next = t;
    } else l = o = t;
    n = { baseState: r.baseState, firstBaseUpdate: l, lastBaseUpdate: o, shared: r.shared, effects: r.effects }, e.updateQueue = n;
    return;
  }
  e = n.lastBaseUpdate, e === null ? n.firstBaseUpdate = t : e.next = t, n.lastBaseUpdate = t;
}
function gl(e, t, n, r) {
  var l = e.updateQueue;
  vt = !1;
  var o = l.firstBaseUpdate, i = l.lastBaseUpdate, s = l.shared.pending;
  if (s !== null) {
    l.shared.pending = null;
    var u = s, a = u.next;
    u.next = null, i === null ? o = a : i.next = a, i = u;
    var p = e.alternate;
    p !== null && (p = p.updateQueue, s = p.lastBaseUpdate, s !== i && (s === null ? p.firstBaseUpdate = a : s.next = a, p.lastBaseUpdate = u));
  }
  if (o !== null) {
    var h = l.baseState;
    i = 0, p = a = u = null, s = o;
    do {
      var m = s.lane, x = s.eventTime;
      if ((r & m) === m) {
        p !== null && (p = p.next = {
          eventTime: x,
          lane: 0,
          tag: s.tag,
          payload: s.payload,
          callback: s.callback,
          next: null
        });
        e: {
          var _ = e, k = s;
          switch (m = t, x = n, k.tag) {
            case 1:
              if (_ = k.payload, typeof _ == "function") {
                h = _.call(x, h, m);
                break e;
              }
              h = _;
              break e;
            case 3:
              _.flags = _.flags & -65537 | 128;
            case 0:
              if (_ = k.payload, m = typeof _ == "function" ? _.call(x, h, m) : _, m == null) break e;
              h = J({}, h, m);
              break e;
            case 2:
              vt = !0;
          }
        }
        s.callback !== null && s.lane !== 0 && (e.flags |= 64, m = l.effects, m === null ? l.effects = [s] : m.push(s));
      } else x = { eventTime: x, lane: m, tag: s.tag, payload: s.payload, callback: s.callback, next: null }, p === null ? (a = p = x, u = h) : p = p.next = x, i |= m;
      if (s = s.next, s === null) {
        if (s = l.shared.pending, s === null) break;
        m = s, s = m.next, m.next = null, l.lastBaseUpdate = m, l.shared.pending = null;
      }
    } while (!0);
    if (p === null && (u = h), l.baseState = u, l.firstBaseUpdate = a, l.lastBaseUpdate = p, t = l.shared.interleaved, t !== null) {
      l = t;
      do
        i |= l.lane, l = l.next;
      while (l !== t);
    } else o === null && (l.shared.lanes = 0);
    Kt |= i, e.lanes = i, e.memoizedState = h;
  }
}
function mu(e, t, n) {
  if (e = t.effects, t.effects = null, e !== null) for (t = 0; t < e.length; t++) {
    var r = e[t], l = r.callback;
    if (l !== null) {
      if (r.callback = null, r = n, typeof l != "function") throw Error(j(191, l));
      l.call(r);
    }
  }
}
var Sr = {}, et = Mt(Sr), fr = Mt(Sr), dr = Mt(Sr);
function Ut(e) {
  if (e === Sr) throw Error(j(174));
  return e;
}
function ss(e, t) {
  switch (B(dr, t), B(fr, e), B(et, Sr), e = t.nodeType, e) {
    case 9:
    case 11:
      t = (t = t.documentElement) ? t.namespaceURI : Ao(null, "");
      break;
    default:
      e = e === 8 ? t.parentNode : t, t = e.namespaceURI || null, e = e.tagName, t = Ao(t, e);
  }
  K(et), B(et, t);
}
function Sn() {
  K(et), K(fr), K(dr);
}
function pc(e) {
  Ut(dr.current);
  var t = Ut(et.current), n = Ao(t, e.type);
  t !== n && (B(fr, e), B(et, n));
}
function us(e) {
  fr.current === e && (K(et), K(fr));
}
var Y = Mt(0);
function yl(e) {
  for (var t = e; t !== null; ) {
    if (t.tag === 13) {
      var n = t.memoizedState;
      if (n !== null && (n = n.dehydrated, n === null || n.data === "$?" || n.data === "$!")) return t;
    } else if (t.tag === 19 && t.memoizedProps.revealOrder !== void 0) {
      if (t.flags & 128) return t;
    } else if (t.child !== null) {
      t.child.return = t, t = t.child;
      continue;
    }
    if (t === e) break;
    for (; t.sibling === null; ) {
      if (t.return === null || t.return === e) return null;
      t = t.return;
    }
    t.sibling.return = t.return, t = t.sibling;
  }
  return null;
}
var vo = [];
function as() {
  for (var e = 0; e < vo.length; e++) vo[e]._workInProgressVersionPrimary = null;
  vo.length = 0;
}
var Jr = dt.ReactCurrentDispatcher, go = dt.ReactCurrentBatchConfig, Qt = 0, G = null, ne = null, oe = null, xl = !1, Jn = !1, pr = 0, pp = 0;
function pe() {
  throw Error(j(321));
}
function cs(e, t) {
  if (t === null) return !1;
  for (var n = 0; n < t.length && n < e.length; n++) if (!Ye(e[n], t[n])) return !1;
  return !0;
}
function fs(e, t, n, r, l, o) {
  if (Qt = o, G = t, t.memoizedState = null, t.updateQueue = null, t.lanes = 0, Jr.current = e === null || e.memoizedState === null ? gp : yp, e = n(r, l), Jn) {
    o = 0;
    do {
      if (Jn = !1, pr = 0, 25 <= o) throw Error(j(301));
      o += 1, oe = ne = null, t.updateQueue = null, Jr.current = xp, e = n(r, l);
    } while (Jn);
  }
  if (Jr.current = wl, t = ne !== null && ne.next !== null, Qt = 0, oe = ne = G = null, xl = !1, t) throw Error(j(300));
  return e;
}
function ds() {
  var e = pr !== 0;
  return pr = 0, e;
}
function Ze() {
  var e = { memoizedState: null, baseState: null, baseQueue: null, queue: null, next: null };
  return oe === null ? G.memoizedState = oe = e : oe = oe.next = e, oe;
}
function Ue() {
  if (ne === null) {
    var e = G.alternate;
    e = e !== null ? e.memoizedState : null;
  } else e = ne.next;
  var t = oe === null ? G.memoizedState : oe.next;
  if (t !== null) oe = t, ne = e;
  else {
    if (e === null) throw Error(j(310));
    ne = e, e = { memoizedState: ne.memoizedState, baseState: ne.baseState, baseQueue: ne.baseQueue, queue: ne.queue, next: null }, oe === null ? G.memoizedState = oe = e : oe = oe.next = e;
  }
  return oe;
}
function mr(e, t) {
  return typeof t == "function" ? t(e) : t;
}
function yo(e) {
  var t = Ue(), n = t.queue;
  if (n === null) throw Error(j(311));
  n.lastRenderedReducer = e;
  var r = ne, l = r.baseQueue, o = n.pending;
  if (o !== null) {
    if (l !== null) {
      var i = l.next;
      l.next = o.next, o.next = i;
    }
    r.baseQueue = l = o, n.pending = null;
  }
  if (l !== null) {
    o = l.next, r = r.baseState;
    var s = i = null, u = null, a = o;
    do {
      var p = a.lane;
      if ((Qt & p) === p) u !== null && (u = u.next = { lane: 0, action: a.action, hasEagerState: a.hasEagerState, eagerState: a.eagerState, next: null }), r = a.hasEagerState ? a.eagerState : e(r, a.action);
      else {
        var h = {
          lane: p,
          action: a.action,
          hasEagerState: a.hasEagerState,
          eagerState: a.eagerState,
          next: null
        };
        u === null ? (s = u = h, i = r) : u = u.next = h, G.lanes |= p, Kt |= p;
      }
      a = a.next;
    } while (a !== null && a !== o);
    u === null ? i = r : u.next = s, Ye(r, t.memoizedState) || (ke = !0), t.memoizedState = r, t.baseState = i, t.baseQueue = u, n.lastRenderedState = r;
  }
  if (e = n.interleaved, e !== null) {
    l = e;
    do
      o = l.lane, G.lanes |= o, Kt |= o, l = l.next;
    while (l !== e);
  } else l === null && (n.lanes = 0);
  return [t.memoizedState, n.dispatch];
}
function xo(e) {
  var t = Ue(), n = t.queue;
  if (n === null) throw Error(j(311));
  n.lastRenderedReducer = e;
  var r = n.dispatch, l = n.pending, o = t.memoizedState;
  if (l !== null) {
    n.pending = null;
    var i = l = l.next;
    do
      o = e(o, i.action), i = i.next;
    while (i !== l);
    Ye(o, t.memoizedState) || (ke = !0), t.memoizedState = o, t.baseQueue === null && (t.baseState = o), n.lastRenderedState = o;
  }
  return [o, r];
}
function mc() {
}
function hc(e, t) {
  var n = G, r = Ue(), l = t(), o = !Ye(r.memoizedState, l);
  if (o && (r.memoizedState = l, ke = !0), r = r.queue, ps(yc.bind(null, n, r, e), [e]), r.getSnapshot !== t || o || oe !== null && oe.memoizedState.tag & 1) {
    if (n.flags |= 2048, hr(9, gc.bind(null, n, r, l, t), void 0, null), ie === null) throw Error(j(349));
    Qt & 30 || vc(n, t, l);
  }
  return l;
}
function vc(e, t, n) {
  e.flags |= 16384, e = { getSnapshot: t, value: n }, t = G.updateQueue, t === null ? (t = { lastEffect: null, stores: null }, G.updateQueue = t, t.stores = [e]) : (n = t.stores, n === null ? t.stores = [e] : n.push(e));
}
function gc(e, t, n, r) {
  t.value = n, t.getSnapshot = r, xc(t) && wc(e);
}
function yc(e, t, n) {
  return n(function() {
    xc(t) && wc(e);
  });
}
function xc(e) {
  var t = e.getSnapshot;
  e = e.value;
  try {
    var n = t();
    return !Ye(e, n);
  } catch {
    return !0;
  }
}
function wc(e) {
  var t = ct(e, 1);
  t !== null && Xe(t, e, 1, -1);
}
function hu(e) {
  var t = Ze();
  return typeof e == "function" && (e = e()), t.memoizedState = t.baseState = e, e = { pending: null, interleaved: null, lanes: 0, dispatch: null, lastRenderedReducer: mr, lastRenderedState: e }, t.queue = e, e = e.dispatch = vp.bind(null, G, e), [t.memoizedState, e];
}
function hr(e, t, n, r) {
  return e = { tag: e, create: t, destroy: n, deps: r, next: null }, t = G.updateQueue, t === null ? (t = { lastEffect: null, stores: null }, G.updateQueue = t, t.lastEffect = e.next = e) : (n = t.lastEffect, n === null ? t.lastEffect = e.next = e : (r = n.next, n.next = e, e.next = r, t.lastEffect = e)), e;
}
function _c() {
  return Ue().memoizedState;
}
function Zr(e, t, n, r) {
  var l = Ze();
  G.flags |= e, l.memoizedState = hr(1 | t, n, void 0, r === void 0 ? null : r);
}
function Dl(e, t, n, r) {
  var l = Ue();
  r = r === void 0 ? null : r;
  var o = void 0;
  if (ne !== null) {
    var i = ne.memoizedState;
    if (o = i.destroy, r !== null && cs(r, i.deps)) {
      l.memoizedState = hr(t, n, o, r);
      return;
    }
  }
  G.flags |= e, l.memoizedState = hr(1 | t, n, o, r);
}
function vu(e, t) {
  return Zr(8390656, 8, e, t);
}
function ps(e, t) {
  return Dl(2048, 8, e, t);
}
function jc(e, t) {
  return Dl(4, 2, e, t);
}
function kc(e, t) {
  return Dl(4, 4, e, t);
}
function Sc(e, t) {
  if (typeof t == "function") return e = e(), t(e), function() {
    t(null);
  };
  if (t != null) return e = e(), t.current = e, function() {
    t.current = null;
  };
}
function Cc(e, t, n) {
  return n = n != null ? n.concat([e]) : null, Dl(4, 4, Sc.bind(null, t, e), n);
}
function ms() {
}
function Ec(e, t) {
  var n = Ue();
  t = t === void 0 ? null : t;
  var r = n.memoizedState;
  return r !== null && t !== null && cs(t, r[1]) ? r[0] : (n.memoizedState = [e, t], e);
}
function Fc(e, t) {
  var n = Ue();
  t = t === void 0 ? null : t;
  var r = n.memoizedState;
  return r !== null && t !== null && cs(t, r[1]) ? r[0] : (e = e(), n.memoizedState = [e, t], e);
}
function Nc(e, t, n) {
  return Qt & 21 ? (Ye(n, t) || (n = La(), G.lanes |= n, Kt |= n, e.baseState = !0), t) : (e.baseState && (e.baseState = !1, ke = !0), e.memoizedState = n);
}
function mp(e, t) {
  var n = A;
  A = n !== 0 && 4 > n ? n : 4, e(!0);
  var r = go.transition;
  go.transition = {};
  try {
    e(!1), t();
  } finally {
    A = n, go.transition = r;
  }
}
function Tc() {
  return Ue().memoizedState;
}
function hp(e, t, n) {
  var r = Ft(e);
  if (n = { lane: r, action: n, hasEagerState: !1, eagerState: null, next: null }, Pc(e)) zc(t, n);
  else if (n = fc(e, t, n, r), n !== null) {
    var l = ye();
    Xe(n, e, r, l), Mc(n, t, r);
  }
}
function vp(e, t, n) {
  var r = Ft(e), l = { lane: r, action: n, hasEagerState: !1, eagerState: null, next: null };
  if (Pc(e)) zc(t, l);
  else {
    var o = e.alternate;
    if (e.lanes === 0 && (o === null || o.lanes === 0) && (o = t.lastRenderedReducer, o !== null)) try {
      var i = t.lastRenderedState, s = o(i, n);
      if (l.hasEagerState = !0, l.eagerState = s, Ye(s, i)) {
        var u = t.interleaved;
        u === null ? (l.next = l, os(t)) : (l.next = u.next, u.next = l), t.interleaved = l;
        return;
      }
    } catch {
    } finally {
    }
    n = fc(e, t, l, r), n !== null && (l = ye(), Xe(n, e, r, l), Mc(n, t, r));
  }
}
function Pc(e) {
  var t = e.alternate;
  return e === G || t !== null && t === G;
}
function zc(e, t) {
  Jn = xl = !0;
  var n = e.pending;
  n === null ? t.next = t : (t.next = n.next, n.next = t), e.pending = t;
}
function Mc(e, t, n) {
  if (n & 4194240) {
    var r = t.lanes;
    r &= e.pendingLanes, n |= r, t.lanes = n, Qi(e, n);
  }
}
var wl = { readContext: Ae, useCallback: pe, useContext: pe, useEffect: pe, useImperativeHandle: pe, useInsertionEffect: pe, useLayoutEffect: pe, useMemo: pe, useReducer: pe, useRef: pe, useState: pe, useDebugValue: pe, useDeferredValue: pe, useTransition: pe, useMutableSource: pe, useSyncExternalStore: pe, useId: pe, unstable_isNewReconciler: !1 }, gp = { readContext: Ae, useCallback: function(e, t) {
  return Ze().memoizedState = [e, t === void 0 ? null : t], e;
}, useContext: Ae, useEffect: vu, useImperativeHandle: function(e, t, n) {
  return n = n != null ? n.concat([e]) : null, Zr(
    4194308,
    4,
    Sc.bind(null, t, e),
    n
  );
}, useLayoutEffect: function(e, t) {
  return Zr(4194308, 4, e, t);
}, useInsertionEffect: function(e, t) {
  return Zr(4, 2, e, t);
}, useMemo: function(e, t) {
  var n = Ze();
  return t = t === void 0 ? null : t, e = e(), n.memoizedState = [e, t], e;
}, useReducer: function(e, t, n) {
  var r = Ze();
  return t = n !== void 0 ? n(t) : t, r.memoizedState = r.baseState = t, e = { pending: null, interleaved: null, lanes: 0, dispatch: null, lastRenderedReducer: e, lastRenderedState: t }, r.queue = e, e = e.dispatch = hp.bind(null, G, e), [r.memoizedState, e];
}, useRef: function(e) {
  var t = Ze();
  return e = { current: e }, t.memoizedState = e;
}, useState: hu, useDebugValue: ms, useDeferredValue: function(e) {
  return Ze().memoizedState = e;
}, useTransition: function() {
  var e = hu(!1), t = e[0];
  return e = mp.bind(null, e[1]), Ze().memoizedState = e, [t, e];
}, useMutableSource: function() {
}, useSyncExternalStore: function(e, t, n) {
  var r = G, l = Ze();
  if (X) {
    if (n === void 0) throw Error(j(407));
    n = n();
  } else {
    if (n = t(), ie === null) throw Error(j(349));
    Qt & 30 || vc(r, t, n);
  }
  l.memoizedState = n;
  var o = { value: n, getSnapshot: t };
  return l.queue = o, vu(yc.bind(
    null,
    r,
    o,
    e
  ), [e]), r.flags |= 2048, hr(9, gc.bind(null, r, o, n, t), void 0, null), n;
}, useId: function() {
  var e = Ze(), t = ie.identifierPrefix;
  if (X) {
    var n = it, r = ot;
    n = (r & ~(1 << 32 - Ke(r) - 1)).toString(32) + n, t = ":" + t + "R" + n, n = pr++, 0 < n && (t += "H" + n.toString(32)), t += ":";
  } else n = pp++, t = ":" + t + "r" + n.toString(32) + ":";
  return e.memoizedState = t;
}, unstable_isNewReconciler: !1 }, yp = {
  readContext: Ae,
  useCallback: Ec,
  useContext: Ae,
  useEffect: ps,
  useImperativeHandle: Cc,
  useInsertionEffect: jc,
  useLayoutEffect: kc,
  useMemo: Fc,
  useReducer: yo,
  useRef: _c,
  useState: function() {
    return yo(mr);
  },
  useDebugValue: ms,
  useDeferredValue: function(e) {
    var t = Ue();
    return Nc(t, ne.memoizedState, e);
  },
  useTransition: function() {
    var e = yo(mr)[0], t = Ue().memoizedState;
    return [e, t];
  },
  useMutableSource: mc,
  useSyncExternalStore: hc,
  useId: Tc,
  unstable_isNewReconciler: !1
}, xp = { readContext: Ae, useCallback: Ec, useContext: Ae, useEffect: ps, useImperativeHandle: Cc, useInsertionEffect: jc, useLayoutEffect: kc, useMemo: Fc, useReducer: xo, useRef: _c, useState: function() {
  return xo(mr);
}, useDebugValue: ms, useDeferredValue: function(e) {
  var t = Ue();
  return ne === null ? t.memoizedState = e : Nc(t, ne.memoizedState, e);
}, useTransition: function() {
  var e = xo(mr)[0], t = Ue().memoizedState;
  return [e, t];
}, useMutableSource: mc, useSyncExternalStore: hc, useId: Tc, unstable_isNewReconciler: !1 };
function He(e, t) {
  if (e && e.defaultProps) {
    t = J({}, t), e = e.defaultProps;
    for (var n in e) t[n] === void 0 && (t[n] = e[n]);
    return t;
  }
  return t;
}
function ii(e, t, n, r) {
  t = e.memoizedState, n = n(r, t), n = n == null ? t : J({}, t, n), e.memoizedState = n, e.lanes === 0 && (e.updateQueue.baseState = n);
}
var Wl = { isMounted: function(e) {
  return (e = e._reactInternals) ? Zt(e) === e : !1;
}, enqueueSetState: function(e, t, n) {
  e = e._reactInternals;
  var r = ye(), l = Ft(e), o = st(r, l);
  o.payload = t, n != null && (o.callback = n), t = Ct(e, o, l), t !== null && (Xe(t, e, l, r), Gr(t, e, l));
}, enqueueReplaceState: function(e, t, n) {
  e = e._reactInternals;
  var r = ye(), l = Ft(e), o = st(r, l);
  o.tag = 1, o.payload = t, n != null && (o.callback = n), t = Ct(e, o, l), t !== null && (Xe(t, e, l, r), Gr(t, e, l));
}, enqueueForceUpdate: function(e, t) {
  e = e._reactInternals;
  var n = ye(), r = Ft(e), l = st(n, r);
  l.tag = 2, t != null && (l.callback = t), t = Ct(e, l, r), t !== null && (Xe(t, e, r, n), Gr(t, e, r));
} };
function gu(e, t, n, r, l, o, i) {
  return e = e.stateNode, typeof e.shouldComponentUpdate == "function" ? e.shouldComponentUpdate(r, o, i) : t.prototype && t.prototype.isPureReactComponent ? !sr(n, r) || !sr(l, o) : !0;
}
function Lc(e, t, n) {
  var r = !1, l = Pt, o = t.contextType;
  return typeof o == "object" && o !== null ? o = Ae(o) : (l = Ce(t) ? Bt : ve.current, r = t.contextTypes, o = (r = r != null) ? _n(e, l) : Pt), t = new t(n, o), e.memoizedState = t.state !== null && t.state !== void 0 ? t.state : null, t.updater = Wl, e.stateNode = t, t._reactInternals = e, r && (e = e.stateNode, e.__reactInternalMemoizedUnmaskedChildContext = l, e.__reactInternalMemoizedMaskedChildContext = o), t;
}
function yu(e, t, n, r) {
  e = t.state, typeof t.componentWillReceiveProps == "function" && t.componentWillReceiveProps(n, r), typeof t.UNSAFE_componentWillReceiveProps == "function" && t.UNSAFE_componentWillReceiveProps(n, r), t.state !== e && Wl.enqueueReplaceState(t, t.state, null);
}
function si(e, t, n, r) {
  var l = e.stateNode;
  l.props = n, l.state = e.memoizedState, l.refs = {}, is(e);
  var o = t.contextType;
  typeof o == "object" && o !== null ? l.context = Ae(o) : (o = Ce(t) ? Bt : ve.current, l.context = _n(e, o)), l.state = e.memoizedState, o = t.getDerivedStateFromProps, typeof o == "function" && (ii(e, t, o, n), l.state = e.memoizedState), typeof t.getDerivedStateFromProps == "function" || typeof l.getSnapshotBeforeUpdate == "function" || typeof l.UNSAFE_componentWillMount != "function" && typeof l.componentWillMount != "function" || (t = l.state, typeof l.componentWillMount == "function" && l.componentWillMount(), typeof l.UNSAFE_componentWillMount == "function" && l.UNSAFE_componentWillMount(), t !== l.state && Wl.enqueueReplaceState(l, l.state, null), gl(e, n, l, r), l.state = e.memoizedState), typeof l.componentDidMount == "function" && (e.flags |= 4194308);
}
function Cn(e, t) {
  try {
    var n = "", r = t;
    do
      n += Xf(r), r = r.return;
    while (r);
    var l = n;
  } catch (o) {
    l = `
Error generating stack: ` + o.message + `
` + o.stack;
  }
  return { value: e, source: t, stack: l, digest: null };
}
function wo(e, t, n) {
  return { value: e, source: null, stack: n ?? null, digest: t ?? null };
}
function ui(e, t) {
  try {
    console.error(t.value);
  } catch (n) {
    setTimeout(function() {
      throw n;
    });
  }
}
var wp = typeof WeakMap == "function" ? WeakMap : Map;
function Rc(e, t, n) {
  n = st(-1, n), n.tag = 3, n.payload = { element: null };
  var r = t.value;
  return n.callback = function() {
    jl || (jl = !0, yi = r), ui(e, t);
  }, n;
}
function Ic(e, t, n) {
  n = st(-1, n), n.tag = 3;
  var r = e.type.getDerivedStateFromError;
  if (typeof r == "function") {
    var l = t.value;
    n.payload = function() {
      return r(l);
    }, n.callback = function() {
      ui(e, t);
    };
  }
  var o = e.stateNode;
  return o !== null && typeof o.componentDidCatch == "function" && (n.callback = function() {
    ui(e, t), typeof r != "function" && (Et === null ? Et = /* @__PURE__ */ new Set([this]) : Et.add(this));
    var i = t.stack;
    this.componentDidCatch(t.value, { componentStack: i !== null ? i : "" });
  }), n;
}
function xu(e, t, n) {
  var r = e.pingCache;
  if (r === null) {
    r = e.pingCache = new wp();
    var l = /* @__PURE__ */ new Set();
    r.set(t, l);
  } else l = r.get(t), l === void 0 && (l = /* @__PURE__ */ new Set(), r.set(t, l));
  l.has(n) || (l.add(n), e = Rp.bind(null, e, t, n), t.then(e, e));
}
function wu(e) {
  do {
    var t;
    if ((t = e.tag === 13) && (t = e.memoizedState, t = t !== null ? t.dehydrated !== null : !0), t) return e;
    e = e.return;
  } while (e !== null);
  return null;
}
function _u(e, t, n, r, l) {
  return e.mode & 1 ? (e.flags |= 65536, e.lanes = l, e) : (e === t ? e.flags |= 65536 : (e.flags |= 128, n.flags |= 131072, n.flags &= -52805, n.tag === 1 && (n.alternate === null ? n.tag = 17 : (t = st(-1, 1), t.tag = 2, Ct(n, t, 1))), n.lanes |= 1), e);
}
var _p = dt.ReactCurrentOwner, ke = !1;
function ge(e, t, n, r) {
  t.child = e === null ? cc(t, null, n, r) : kn(t, e.child, n, r);
}
function ju(e, t, n, r, l) {
  n = n.render;
  var o = t.ref;
  return yn(t, l), r = fs(e, t, n, r, o, l), n = ds(), e !== null && !ke ? (t.updateQueue = e.updateQueue, t.flags &= -2053, e.lanes &= ~l, ft(e, t, l)) : (X && n && bi(t), t.flags |= 1, ge(e, t, r, l), t.child);
}
function ku(e, t, n, r, l) {
  if (e === null) {
    var o = n.type;
    return typeof o == "function" && !js(o) && o.defaultProps === void 0 && n.compare === null && n.defaultProps === void 0 ? (t.tag = 15, t.type = o, Oc(e, t, o, r, l)) : (e = tl(n.type, null, r, t, t.mode, l), e.ref = t.ref, e.return = t, t.child = e);
  }
  if (o = e.child, !(e.lanes & l)) {
    var i = o.memoizedProps;
    if (n = n.compare, n = n !== null ? n : sr, n(i, r) && e.ref === t.ref) return ft(e, t, l);
  }
  return t.flags |= 1, e = Nt(o, r), e.ref = t.ref, e.return = t, t.child = e;
}
function Oc(e, t, n, r, l) {
  if (e !== null) {
    var o = e.memoizedProps;
    if (sr(o, r) && e.ref === t.ref) if (ke = !1, t.pendingProps = r = o, (e.lanes & l) !== 0) e.flags & 131072 && (ke = !0);
    else return t.lanes = e.lanes, ft(e, t, l);
  }
  return ai(e, t, n, r, l);
}
function Dc(e, t, n) {
  var r = t.pendingProps, l = r.children, o = e !== null ? e.memoizedState : null;
  if (r.mode === "hidden") if (!(t.mode & 1)) t.memoizedState = { baseLanes: 0, cachePool: null, transitions: null }, B(pn, Ne), Ne |= n;
  else {
    if (!(n & 1073741824)) return e = o !== null ? o.baseLanes | n : n, t.lanes = t.childLanes = 1073741824, t.memoizedState = { baseLanes: e, cachePool: null, transitions: null }, t.updateQueue = null, B(pn, Ne), Ne |= e, null;
    t.memoizedState = { baseLanes: 0, cachePool: null, transitions: null }, r = o !== null ? o.baseLanes : n, B(pn, Ne), Ne |= r;
  }
  else o !== null ? (r = o.baseLanes | n, t.memoizedState = null) : r = n, B(pn, Ne), Ne |= r;
  return ge(e, t, l, n), t.child;
}
function Wc(e, t) {
  var n = t.ref;
  (e === null && n !== null || e !== null && e.ref !== n) && (t.flags |= 512, t.flags |= 2097152);
}
function ai(e, t, n, r, l) {
  var o = Ce(n) ? Bt : ve.current;
  return o = _n(t, o), yn(t, l), n = fs(e, t, n, r, o, l), r = ds(), e !== null && !ke ? (t.updateQueue = e.updateQueue, t.flags &= -2053, e.lanes &= ~l, ft(e, t, l)) : (X && r && bi(t), t.flags |= 1, ge(e, t, n, l), t.child);
}
function Su(e, t, n, r, l) {
  if (Ce(n)) {
    var o = !0;
    dl(t);
  } else o = !1;
  if (yn(t, l), t.stateNode === null) qr(e, t), Lc(t, n, r), si(t, n, r, l), r = !0;
  else if (e === null) {
    var i = t.stateNode, s = t.memoizedProps;
    i.props = s;
    var u = i.context, a = n.contextType;
    typeof a == "object" && a !== null ? a = Ae(a) : (a = Ce(n) ? Bt : ve.current, a = _n(t, a));
    var p = n.getDerivedStateFromProps, h = typeof p == "function" || typeof i.getSnapshotBeforeUpdate == "function";
    h || typeof i.UNSAFE_componentWillReceiveProps != "function" && typeof i.componentWillReceiveProps != "function" || (s !== r || u !== a) && yu(t, i, r, a), vt = !1;
    var m = t.memoizedState;
    i.state = m, gl(t, r, i, l), u = t.memoizedState, s !== r || m !== u || Se.current || vt ? (typeof p == "function" && (ii(t, n, p, r), u = t.memoizedState), (s = vt || gu(t, n, s, r, m, u, a)) ? (h || typeof i.UNSAFE_componentWillMount != "function" && typeof i.componentWillMount != "function" || (typeof i.componentWillMount == "function" && i.componentWillMount(), typeof i.UNSAFE_componentWillMount == "function" && i.UNSAFE_componentWillMount()), typeof i.componentDidMount == "function" && (t.flags |= 4194308)) : (typeof i.componentDidMount == "function" && (t.flags |= 4194308), t.memoizedProps = r, t.memoizedState = u), i.props = r, i.state = u, i.context = a, r = s) : (typeof i.componentDidMount == "function" && (t.flags |= 4194308), r = !1);
  } else {
    i = t.stateNode, dc(e, t), s = t.memoizedProps, a = t.type === t.elementType ? s : He(t.type, s), i.props = a, h = t.pendingProps, m = i.context, u = n.contextType, typeof u == "object" && u !== null ? u = Ae(u) : (u = Ce(n) ? Bt : ve.current, u = _n(t, u));
    var x = n.getDerivedStateFromProps;
    (p = typeof x == "function" || typeof i.getSnapshotBeforeUpdate == "function") || typeof i.UNSAFE_componentWillReceiveProps != "function" && typeof i.componentWillReceiveProps != "function" || (s !== h || m !== u) && yu(t, i, r, u), vt = !1, m = t.memoizedState, i.state = m, gl(t, r, i, l);
    var _ = t.memoizedState;
    s !== h || m !== _ || Se.current || vt ? (typeof x == "function" && (ii(t, n, x, r), _ = t.memoizedState), (a = vt || gu(t, n, a, r, m, _, u) || !1) ? (p || typeof i.UNSAFE_componentWillUpdate != "function" && typeof i.componentWillUpdate != "function" || (typeof i.componentWillUpdate == "function" && i.componentWillUpdate(r, _, u), typeof i.UNSAFE_componentWillUpdate == "function" && i.UNSAFE_componentWillUpdate(r, _, u)), typeof i.componentDidUpdate == "function" && (t.flags |= 4), typeof i.getSnapshotBeforeUpdate == "function" && (t.flags |= 1024)) : (typeof i.componentDidUpdate != "function" || s === e.memoizedProps && m === e.memoizedState || (t.flags |= 4), typeof i.getSnapshotBeforeUpdate != "function" || s === e.memoizedProps && m === e.memoizedState || (t.flags |= 1024), t.memoizedProps = r, t.memoizedState = _), i.props = r, i.state = _, i.context = u, r = a) : (typeof i.componentDidUpdate != "function" || s === e.memoizedProps && m === e.memoizedState || (t.flags |= 4), typeof i.getSnapshotBeforeUpdate != "function" || s === e.memoizedProps && m === e.memoizedState || (t.flags |= 1024), r = !1);
  }
  return ci(e, t, n, r, o, l);
}
function ci(e, t, n, r, l, o) {
  Wc(e, t);
  var i = (t.flags & 128) !== 0;
  if (!r && !i) return l && au(t, n, !1), ft(e, t, o);
  r = t.stateNode, _p.current = t;
  var s = i && typeof n.getDerivedStateFromError != "function" ? null : r.render();
  return t.flags |= 1, e !== null && i ? (t.child = kn(t, e.child, null, o), t.child = kn(t, null, s, o)) : ge(e, t, s, o), t.memoizedState = r.state, l && au(t, n, !0), t.child;
}
function Ac(e) {
  var t = e.stateNode;
  t.pendingContext ? uu(e, t.pendingContext, t.pendingContext !== t.context) : t.context && uu(e, t.context, !1), ss(e, t.containerInfo);
}
function Cu(e, t, n, r, l) {
  return jn(), ts(l), t.flags |= 256, ge(e, t, n, r), t.child;
}
var fi = { dehydrated: null, treeContext: null, retryLane: 0 };
function di(e) {
  return { baseLanes: e, cachePool: null, transitions: null };
}
function Uc(e, t, n) {
  var r = t.pendingProps, l = Y.current, o = !1, i = (t.flags & 128) !== 0, s;
  if ((s = i) || (s = e !== null && e.memoizedState === null ? !1 : (l & 2) !== 0), s ? (o = !0, t.flags &= -129) : (e === null || e.memoizedState !== null) && (l |= 1), B(Y, l & 1), e === null)
    return li(t), e = t.memoizedState, e !== null && (e = e.dehydrated, e !== null) ? (t.mode & 1 ? e.data === "$!" ? t.lanes = 8 : t.lanes = 1073741824 : t.lanes = 1, null) : (i = r.children, e = r.fallback, o ? (r = t.mode, o = t.child, i = { mode: "hidden", children: i }, !(r & 1) && o !== null ? (o.childLanes = 0, o.pendingProps = i) : o = $l(i, r, 0, null), e = Ht(e, r, n, null), o.return = t, e.return = t, o.sibling = e, t.child = o, t.child.memoizedState = di(n), t.memoizedState = fi, e) : hs(t, i));
  if (l = e.memoizedState, l !== null && (s = l.dehydrated, s !== null)) return jp(e, t, i, r, s, l, n);
  if (o) {
    o = r.fallback, i = t.mode, l = e.child, s = l.sibling;
    var u = { mode: "hidden", children: r.children };
    return !(i & 1) && t.child !== l ? (r = t.child, r.childLanes = 0, r.pendingProps = u, t.deletions = null) : (r = Nt(l, u), r.subtreeFlags = l.subtreeFlags & 14680064), s !== null ? o = Nt(s, o) : (o = Ht(o, i, n, null), o.flags |= 2), o.return = t, r.return = t, r.sibling = o, t.child = r, r = o, o = t.child, i = e.child.memoizedState, i = i === null ? di(n) : { baseLanes: i.baseLanes | n, cachePool: null, transitions: i.transitions }, o.memoizedState = i, o.childLanes = e.childLanes & ~n, t.memoizedState = fi, r;
  }
  return o = e.child, e = o.sibling, r = Nt(o, { mode: "visible", children: r.children }), !(t.mode & 1) && (r.lanes = n), r.return = t, r.sibling = null, e !== null && (n = t.deletions, n === null ? (t.deletions = [e], t.flags |= 16) : n.push(e)), t.child = r, t.memoizedState = null, r;
}
function hs(e, t) {
  return t = $l({ mode: "visible", children: t }, e.mode, 0, null), t.return = e, e.child = t;
}
function Ar(e, t, n, r) {
  return r !== null && ts(r), kn(t, e.child, null, n), e = hs(t, t.pendingProps.children), e.flags |= 2, t.memoizedState = null, e;
}
function jp(e, t, n, r, l, o, i) {
  if (n)
    return t.flags & 256 ? (t.flags &= -257, r = wo(Error(j(422))), Ar(e, t, i, r)) : t.memoizedState !== null ? (t.child = e.child, t.flags |= 128, null) : (o = r.fallback, l = t.mode, r = $l({ mode: "visible", children: r.children }, l, 0, null), o = Ht(o, l, i, null), o.flags |= 2, r.return = t, o.return = t, r.sibling = o, t.child = r, t.mode & 1 && kn(t, e.child, null, i), t.child.memoizedState = di(i), t.memoizedState = fi, o);
  if (!(t.mode & 1)) return Ar(e, t, i, null);
  if (l.data === "$!") {
    if (r = l.nextSibling && l.nextSibling.dataset, r) var s = r.dgst;
    return r = s, o = Error(j(419)), r = wo(o, r, void 0), Ar(e, t, i, r);
  }
  if (s = (i & e.childLanes) !== 0, ke || s) {
    if (r = ie, r !== null) {
      switch (i & -i) {
        case 4:
          l = 2;
          break;
        case 16:
          l = 8;
          break;
        case 64:
        case 128:
        case 256:
        case 512:
        case 1024:
        case 2048:
        case 4096:
        case 8192:
        case 16384:
        case 32768:
        case 65536:
        case 131072:
        case 262144:
        case 524288:
        case 1048576:
        case 2097152:
        case 4194304:
        case 8388608:
        case 16777216:
        case 33554432:
        case 67108864:
          l = 32;
          break;
        case 536870912:
          l = 268435456;
          break;
        default:
          l = 0;
      }
      l = l & (r.suspendedLanes | i) ? 0 : l, l !== 0 && l !== o.retryLane && (o.retryLane = l, ct(e, l), Xe(r, e, l, -1));
    }
    return _s(), r = wo(Error(j(421))), Ar(e, t, i, r);
  }
  return l.data === "$?" ? (t.flags |= 128, t.child = e.child, t = Ip.bind(null, e), l._reactRetry = t, null) : (e = o.treeContext, Te = St(l.nextSibling), Pe = t, X = !0, Ve = null, e !== null && (Ie[Oe++] = ot, Ie[Oe++] = it, Ie[Oe++] = Vt, ot = e.id, it = e.overflow, Vt = t), t = hs(t, r.children), t.flags |= 4096, t);
}
function Eu(e, t, n) {
  e.lanes |= t;
  var r = e.alternate;
  r !== null && (r.lanes |= t), oi(e.return, t, n);
}
function _o(e, t, n, r, l) {
  var o = e.memoizedState;
  o === null ? e.memoizedState = { isBackwards: t, rendering: null, renderingStartTime: 0, last: r, tail: n, tailMode: l } : (o.isBackwards = t, o.rendering = null, o.renderingStartTime = 0, o.last = r, o.tail = n, o.tailMode = l);
}
function $c(e, t, n) {
  var r = t.pendingProps, l = r.revealOrder, o = r.tail;
  if (ge(e, t, r.children, n), r = Y.current, r & 2) r = r & 1 | 2, t.flags |= 128;
  else {
    if (e !== null && e.flags & 128) e: for (e = t.child; e !== null; ) {
      if (e.tag === 13) e.memoizedState !== null && Eu(e, n, t);
      else if (e.tag === 19) Eu(e, n, t);
      else if (e.child !== null) {
        e.child.return = e, e = e.child;
        continue;
      }
      if (e === t) break e;
      for (; e.sibling === null; ) {
        if (e.return === null || e.return === t) break e;
        e = e.return;
      }
      e.sibling.return = e.return, e = e.sibling;
    }
    r &= 1;
  }
  if (B(Y, r), !(t.mode & 1)) t.memoizedState = null;
  else switch (l) {
    case "forwards":
      for (n = t.child, l = null; n !== null; ) e = n.alternate, e !== null && yl(e) === null && (l = n), n = n.sibling;
      n = l, n === null ? (l = t.child, t.child = null) : (l = n.sibling, n.sibling = null), _o(t, !1, l, n, o);
      break;
    case "backwards":
      for (n = null, l = t.child, t.child = null; l !== null; ) {
        if (e = l.alternate, e !== null && yl(e) === null) {
          t.child = l;
          break;
        }
        e = l.sibling, l.sibling = n, n = l, l = e;
      }
      _o(t, !0, n, null, o);
      break;
    case "together":
      _o(t, !1, null, null, void 0);
      break;
    default:
      t.memoizedState = null;
  }
  return t.child;
}
function qr(e, t) {
  !(t.mode & 1) && e !== null && (e.alternate = null, t.alternate = null, t.flags |= 2);
}
function ft(e, t, n) {
  if (e !== null && (t.dependencies = e.dependencies), Kt |= t.lanes, !(n & t.childLanes)) return null;
  if (e !== null && t.child !== e.child) throw Error(j(153));
  if (t.child !== null) {
    for (e = t.child, n = Nt(e, e.pendingProps), t.child = n, n.return = t; e.sibling !== null; ) e = e.sibling, n = n.sibling = Nt(e, e.pendingProps), n.return = t;
    n.sibling = null;
  }
  return t.child;
}
function kp(e, t, n) {
  switch (t.tag) {
    case 3:
      Ac(t), jn();
      break;
    case 5:
      pc(t);
      break;
    case 1:
      Ce(t.type) && dl(t);
      break;
    case 4:
      ss(t, t.stateNode.containerInfo);
      break;
    case 10:
      var r = t.type._context, l = t.memoizedProps.value;
      B(hl, r._currentValue), r._currentValue = l;
      break;
    case 13:
      if (r = t.memoizedState, r !== null)
        return r.dehydrated !== null ? (B(Y, Y.current & 1), t.flags |= 128, null) : n & t.child.childLanes ? Uc(e, t, n) : (B(Y, Y.current & 1), e = ft(e, t, n), e !== null ? e.sibling : null);
      B(Y, Y.current & 1);
      break;
    case 19:
      if (r = (n & t.childLanes) !== 0, e.flags & 128) {
        if (r) return $c(e, t, n);
        t.flags |= 128;
      }
      if (l = t.memoizedState, l !== null && (l.rendering = null, l.tail = null, l.lastEffect = null), B(Y, Y.current), r) break;
      return null;
    case 22:
    case 23:
      return t.lanes = 0, Dc(e, t, n);
  }
  return ft(e, t, n);
}
var Hc, pi, Bc, Vc;
Hc = function(e, t) {
  for (var n = t.child; n !== null; ) {
    if (n.tag === 5 || n.tag === 6) e.appendChild(n.stateNode);
    else if (n.tag !== 4 && n.child !== null) {
      n.child.return = n, n = n.child;
      continue;
    }
    if (n === t) break;
    for (; n.sibling === null; ) {
      if (n.return === null || n.return === t) return;
      n = n.return;
    }
    n.sibling.return = n.return, n = n.sibling;
  }
};
pi = function() {
};
Bc = function(e, t, n, r) {
  var l = e.memoizedProps;
  if (l !== r) {
    e = t.stateNode, Ut(et.current);
    var o = null;
    switch (n) {
      case "input":
        l = Io(e, l), r = Io(e, r), o = [];
        break;
      case "select":
        l = J({}, l, { value: void 0 }), r = J({}, r, { value: void 0 }), o = [];
        break;
      case "textarea":
        l = Wo(e, l), r = Wo(e, r), o = [];
        break;
      default:
        typeof l.onClick != "function" && typeof r.onClick == "function" && (e.onclick = cl);
    }
    Uo(n, r);
    var i;
    n = null;
    for (a in l) if (!r.hasOwnProperty(a) && l.hasOwnProperty(a) && l[a] != null) if (a === "style") {
      var s = l[a];
      for (i in s) s.hasOwnProperty(i) && (n || (n = {}), n[i] = "");
    } else a !== "dangerouslySetInnerHTML" && a !== "children" && a !== "suppressContentEditableWarning" && a !== "suppressHydrationWarning" && a !== "autoFocus" && (er.hasOwnProperty(a) ? o || (o = []) : (o = o || []).push(a, null));
    for (a in r) {
      var u = r[a];
      if (s = l != null ? l[a] : void 0, r.hasOwnProperty(a) && u !== s && (u != null || s != null)) if (a === "style") if (s) {
        for (i in s) !s.hasOwnProperty(i) || u && u.hasOwnProperty(i) || (n || (n = {}), n[i] = "");
        for (i in u) u.hasOwnProperty(i) && s[i] !== u[i] && (n || (n = {}), n[i] = u[i]);
      } else n || (o || (o = []), o.push(
        a,
        n
      )), n = u;
      else a === "dangerouslySetInnerHTML" ? (u = u ? u.__html : void 0, s = s ? s.__html : void 0, u != null && s !== u && (o = o || []).push(a, u)) : a === "children" ? typeof u != "string" && typeof u != "number" || (o = o || []).push(a, "" + u) : a !== "suppressContentEditableWarning" && a !== "suppressHydrationWarning" && (er.hasOwnProperty(a) ? (u != null && a === "onScroll" && Q("scroll", e), o || s === u || (o = [])) : (o = o || []).push(a, u));
    }
    n && (o = o || []).push("style", n);
    var a = o;
    (t.updateQueue = a) && (t.flags |= 4);
  }
};
Vc = function(e, t, n, r) {
  n !== r && (t.flags |= 4);
};
function An(e, t) {
  if (!X) switch (e.tailMode) {
    case "hidden":
      t = e.tail;
      for (var n = null; t !== null; ) t.alternate !== null && (n = t), t = t.sibling;
      n === null ? e.tail = null : n.sibling = null;
      break;
    case "collapsed":
      n = e.tail;
      for (var r = null; n !== null; ) n.alternate !== null && (r = n), n = n.sibling;
      r === null ? t || e.tail === null ? e.tail = null : e.tail.sibling = null : r.sibling = null;
  }
}
function me(e) {
  var t = e.alternate !== null && e.alternate.child === e.child, n = 0, r = 0;
  if (t) for (var l = e.child; l !== null; ) n |= l.lanes | l.childLanes, r |= l.subtreeFlags & 14680064, r |= l.flags & 14680064, l.return = e, l = l.sibling;
  else for (l = e.child; l !== null; ) n |= l.lanes | l.childLanes, r |= l.subtreeFlags, r |= l.flags, l.return = e, l = l.sibling;
  return e.subtreeFlags |= r, e.childLanes = n, t;
}
function Sp(e, t, n) {
  var r = t.pendingProps;
  switch (es(t), t.tag) {
    case 2:
    case 16:
    case 15:
    case 0:
    case 11:
    case 7:
    case 8:
    case 12:
    case 9:
    case 14:
      return me(t), null;
    case 1:
      return Ce(t.type) && fl(), me(t), null;
    case 3:
      return r = t.stateNode, Sn(), K(Se), K(ve), as(), r.pendingContext && (r.context = r.pendingContext, r.pendingContext = null), (e === null || e.child === null) && (Dr(t) ? t.flags |= 4 : e === null || e.memoizedState.isDehydrated && !(t.flags & 256) || (t.flags |= 1024, Ve !== null && (_i(Ve), Ve = null))), pi(e, t), me(t), null;
    case 5:
      us(t);
      var l = Ut(dr.current);
      if (n = t.type, e !== null && t.stateNode != null) Bc(e, t, n, r, l), e.ref !== t.ref && (t.flags |= 512, t.flags |= 2097152);
      else {
        if (!r) {
          if (t.stateNode === null) throw Error(j(166));
          return me(t), null;
        }
        if (e = Ut(et.current), Dr(t)) {
          r = t.stateNode, n = t.type;
          var o = t.memoizedProps;
          switch (r[qe] = t, r[cr] = o, e = (t.mode & 1) !== 0, n) {
            case "dialog":
              Q("cancel", r), Q("close", r);
              break;
            case "iframe":
            case "object":
            case "embed":
              Q("load", r);
              break;
            case "video":
            case "audio":
              for (l = 0; l < Vn.length; l++) Q(Vn[l], r);
              break;
            case "source":
              Q("error", r);
              break;
            case "img":
            case "image":
            case "link":
              Q(
                "error",
                r
              ), Q("load", r);
              break;
            case "details":
              Q("toggle", r);
              break;
            case "input":
              Is(r, o), Q("invalid", r);
              break;
            case "select":
              r._wrapperState = { wasMultiple: !!o.multiple }, Q("invalid", r);
              break;
            case "textarea":
              Ds(r, o), Q("invalid", r);
          }
          Uo(n, o), l = null;
          for (var i in o) if (o.hasOwnProperty(i)) {
            var s = o[i];
            i === "children" ? typeof s == "string" ? r.textContent !== s && (o.suppressHydrationWarning !== !0 && Or(r.textContent, s, e), l = ["children", s]) : typeof s == "number" && r.textContent !== "" + s && (o.suppressHydrationWarning !== !0 && Or(
              r.textContent,
              s,
              e
            ), l = ["children", "" + s]) : er.hasOwnProperty(i) && s != null && i === "onScroll" && Q("scroll", r);
          }
          switch (n) {
            case "input":
              Nr(r), Os(r, o, !0);
              break;
            case "textarea":
              Nr(r), Ws(r);
              break;
            case "select":
            case "option":
              break;
            default:
              typeof o.onClick == "function" && (r.onclick = cl);
          }
          r = l, t.updateQueue = r, r !== null && (t.flags |= 4);
        } else {
          i = l.nodeType === 9 ? l : l.ownerDocument, e === "http://www.w3.org/1999/xhtml" && (e = ya(n)), e === "http://www.w3.org/1999/xhtml" ? n === "script" ? (e = i.createElement("div"), e.innerHTML = "<script><\/script>", e = e.removeChild(e.firstChild)) : typeof r.is == "string" ? e = i.createElement(n, { is: r.is }) : (e = i.createElement(n), n === "select" && (i = e, r.multiple ? i.multiple = !0 : r.size && (i.size = r.size))) : e = i.createElementNS(e, n), e[qe] = t, e[cr] = r, Hc(e, t, !1, !1), t.stateNode = e;
          e: {
            switch (i = $o(n, r), n) {
              case "dialog":
                Q("cancel", e), Q("close", e), l = r;
                break;
              case "iframe":
              case "object":
              case "embed":
                Q("load", e), l = r;
                break;
              case "video":
              case "audio":
                for (l = 0; l < Vn.length; l++) Q(Vn[l], e);
                l = r;
                break;
              case "source":
                Q("error", e), l = r;
                break;
              case "img":
              case "image":
              case "link":
                Q(
                  "error",
                  e
                ), Q("load", e), l = r;
                break;
              case "details":
                Q("toggle", e), l = r;
                break;
              case "input":
                Is(e, r), l = Io(e, r), Q("invalid", e);
                break;
              case "option":
                l = r;
                break;
              case "select":
                e._wrapperState = { wasMultiple: !!r.multiple }, l = J({}, r, { value: void 0 }), Q("invalid", e);
                break;
              case "textarea":
                Ds(e, r), l = Wo(e, r), Q("invalid", e);
                break;
              default:
                l = r;
            }
            Uo(n, l), s = l;
            for (o in s) if (s.hasOwnProperty(o)) {
              var u = s[o];
              o === "style" ? _a(e, u) : o === "dangerouslySetInnerHTML" ? (u = u ? u.__html : void 0, u != null && xa(e, u)) : o === "children" ? typeof u == "string" ? (n !== "textarea" || u !== "") && tr(e, u) : typeof u == "number" && tr(e, "" + u) : o !== "suppressContentEditableWarning" && o !== "suppressHydrationWarning" && o !== "autoFocus" && (er.hasOwnProperty(o) ? u != null && o === "onScroll" && Q("scroll", e) : u != null && Ai(e, o, u, i));
            }
            switch (n) {
              case "input":
                Nr(e), Os(e, r, !1);
                break;
              case "textarea":
                Nr(e), Ws(e);
                break;
              case "option":
                r.value != null && e.setAttribute("value", "" + Tt(r.value));
                break;
              case "select":
                e.multiple = !!r.multiple, o = r.value, o != null ? mn(e, !!r.multiple, o, !1) : r.defaultValue != null && mn(
                  e,
                  !!r.multiple,
                  r.defaultValue,
                  !0
                );
                break;
              default:
                typeof l.onClick == "function" && (e.onclick = cl);
            }
            switch (n) {
              case "button":
              case "input":
              case "select":
              case "textarea":
                r = !!r.autoFocus;
                break e;
              case "img":
                r = !0;
                break e;
              default:
                r = !1;
            }
          }
          r && (t.flags |= 4);
        }
        t.ref !== null && (t.flags |= 512, t.flags |= 2097152);
      }
      return me(t), null;
    case 6:
      if (e && t.stateNode != null) Vc(e, t, e.memoizedProps, r);
      else {
        if (typeof r != "string" && t.stateNode === null) throw Error(j(166));
        if (n = Ut(dr.current), Ut(et.current), Dr(t)) {
          if (r = t.stateNode, n = t.memoizedProps, r[qe] = t, (o = r.nodeValue !== n) && (e = Pe, e !== null)) switch (e.tag) {
            case 3:
              Or(r.nodeValue, n, (e.mode & 1) !== 0);
              break;
            case 5:
              e.memoizedProps.suppressHydrationWarning !== !0 && Or(r.nodeValue, n, (e.mode & 1) !== 0);
          }
          o && (t.flags |= 4);
        } else r = (n.nodeType === 9 ? n : n.ownerDocument).createTextNode(r), r[qe] = t, t.stateNode = r;
      }
      return me(t), null;
    case 13:
      if (K(Y), r = t.memoizedState, e === null || e.memoizedState !== null && e.memoizedState.dehydrated !== null) {
        if (X && Te !== null && t.mode & 1 && !(t.flags & 128)) uc(), jn(), t.flags |= 98560, o = !1;
        else if (o = Dr(t), r !== null && r.dehydrated !== null) {
          if (e === null) {
            if (!o) throw Error(j(318));
            if (o = t.memoizedState, o = o !== null ? o.dehydrated : null, !o) throw Error(j(317));
            o[qe] = t;
          } else jn(), !(t.flags & 128) && (t.memoizedState = null), t.flags |= 4;
          me(t), o = !1;
        } else Ve !== null && (_i(Ve), Ve = null), o = !0;
        if (!o) return t.flags & 65536 ? t : null;
      }
      return t.flags & 128 ? (t.lanes = n, t) : (r = r !== null, r !== (e !== null && e.memoizedState !== null) && r && (t.child.flags |= 8192, t.mode & 1 && (e === null || Y.current & 1 ? re === 0 && (re = 3) : _s())), t.updateQueue !== null && (t.flags |= 4), me(t), null);
    case 4:
      return Sn(), pi(e, t), e === null && ur(t.stateNode.containerInfo), me(t), null;
    case 10:
      return ls(t.type._context), me(t), null;
    case 17:
      return Ce(t.type) && fl(), me(t), null;
    case 19:
      if (K(Y), o = t.memoizedState, o === null) return me(t), null;
      if (r = (t.flags & 128) !== 0, i = o.rendering, i === null) if (r) An(o, !1);
      else {
        if (re !== 0 || e !== null && e.flags & 128) for (e = t.child; e !== null; ) {
          if (i = yl(e), i !== null) {
            for (t.flags |= 128, An(o, !1), r = i.updateQueue, r !== null && (t.updateQueue = r, t.flags |= 4), t.subtreeFlags = 0, r = n, n = t.child; n !== null; ) o = n, e = r, o.flags &= 14680066, i = o.alternate, i === null ? (o.childLanes = 0, o.lanes = e, o.child = null, o.subtreeFlags = 0, o.memoizedProps = null, o.memoizedState = null, o.updateQueue = null, o.dependencies = null, o.stateNode = null) : (o.childLanes = i.childLanes, o.lanes = i.lanes, o.child = i.child, o.subtreeFlags = 0, o.deletions = null, o.memoizedProps = i.memoizedProps, o.memoizedState = i.memoizedState, o.updateQueue = i.updateQueue, o.type = i.type, e = i.dependencies, o.dependencies = e === null ? null : { lanes: e.lanes, firstContext: e.firstContext }), n = n.sibling;
            return B(Y, Y.current & 1 | 2), t.child;
          }
          e = e.sibling;
        }
        o.tail !== null && ee() > En && (t.flags |= 128, r = !0, An(o, !1), t.lanes = 4194304);
      }
      else {
        if (!r) if (e = yl(i), e !== null) {
          if (t.flags |= 128, r = !0, n = e.updateQueue, n !== null && (t.updateQueue = n, t.flags |= 4), An(o, !0), o.tail === null && o.tailMode === "hidden" && !i.alternate && !X) return me(t), null;
        } else 2 * ee() - o.renderingStartTime > En && n !== 1073741824 && (t.flags |= 128, r = !0, An(o, !1), t.lanes = 4194304);
        o.isBackwards ? (i.sibling = t.child, t.child = i) : (n = o.last, n !== null ? n.sibling = i : t.child = i, o.last = i);
      }
      return o.tail !== null ? (t = o.tail, o.rendering = t, o.tail = t.sibling, o.renderingStartTime = ee(), t.sibling = null, n = Y.current, B(Y, r ? n & 1 | 2 : n & 1), t) : (me(t), null);
    case 22:
    case 23:
      return ws(), r = t.memoizedState !== null, e !== null && e.memoizedState !== null !== r && (t.flags |= 8192), r && t.mode & 1 ? Ne & 1073741824 && (me(t), t.subtreeFlags & 6 && (t.flags |= 8192)) : me(t), null;
    case 24:
      return null;
    case 25:
      return null;
  }
  throw Error(j(156, t.tag));
}
function Cp(e, t) {
  switch (es(t), t.tag) {
    case 1:
      return Ce(t.type) && fl(), e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
    case 3:
      return Sn(), K(Se), K(ve), as(), e = t.flags, e & 65536 && !(e & 128) ? (t.flags = e & -65537 | 128, t) : null;
    case 5:
      return us(t), null;
    case 13:
      if (K(Y), e = t.memoizedState, e !== null && e.dehydrated !== null) {
        if (t.alternate === null) throw Error(j(340));
        jn();
      }
      return e = t.flags, e & 65536 ? (t.flags = e & -65537 | 128, t) : null;
    case 19:
      return K(Y), null;
    case 4:
      return Sn(), null;
    case 10:
      return ls(t.type._context), null;
    case 22:
    case 23:
      return ws(), null;
    case 24:
      return null;
    default:
      return null;
  }
}
var Ur = !1, he = !1, Ep = typeof WeakSet == "function" ? WeakSet : Set, N = null;
function dn(e, t) {
  var n = e.ref;
  if (n !== null) if (typeof n == "function") try {
    n(null);
  } catch (r) {
    b(e, t, r);
  }
  else n.current = null;
}
function mi(e, t, n) {
  try {
    n();
  } catch (r) {
    b(e, t, r);
  }
}
var Fu = !1;
function Fp(e, t) {
  if (Zo = sl, e = Ga(), qi(e)) {
    if ("selectionStart" in e) var n = { start: e.selectionStart, end: e.selectionEnd };
    else e: {
      n = (n = e.ownerDocument) && n.defaultView || window;
      var r = n.getSelection && n.getSelection();
      if (r && r.rangeCount !== 0) {
        n = r.anchorNode;
        var l = r.anchorOffset, o = r.focusNode;
        r = r.focusOffset;
        try {
          n.nodeType, o.nodeType;
        } catch {
          n = null;
          break e;
        }
        var i = 0, s = -1, u = -1, a = 0, p = 0, h = e, m = null;
        t: for (; ; ) {
          for (var x; h !== n || l !== 0 && h.nodeType !== 3 || (s = i + l), h !== o || r !== 0 && h.nodeType !== 3 || (u = i + r), h.nodeType === 3 && (i += h.nodeValue.length), (x = h.firstChild) !== null; )
            m = h, h = x;
          for (; ; ) {
            if (h === e) break t;
            if (m === n && ++a === l && (s = i), m === o && ++p === r && (u = i), (x = h.nextSibling) !== null) break;
            h = m, m = h.parentNode;
          }
          h = x;
        }
        n = s === -1 || u === -1 ? null : { start: s, end: u };
      } else n = null;
    }
    n = n || { start: 0, end: 0 };
  } else n = null;
  for (qo = { focusedElem: e, selectionRange: n }, sl = !1, N = t; N !== null; ) if (t = N, e = t.child, (t.subtreeFlags & 1028) !== 0 && e !== null) e.return = t, N = e;
  else for (; N !== null; ) {
    t = N;
    try {
      var _ = t.alternate;
      if (t.flags & 1024) switch (t.tag) {
        case 0:
        case 11:
        case 15:
          break;
        case 1:
          if (_ !== null) {
            var k = _.memoizedProps, I = _.memoizedState, f = t.stateNode, c = f.getSnapshotBeforeUpdate(t.elementType === t.type ? k : He(t.type, k), I);
            f.__reactInternalSnapshotBeforeUpdate = c;
          }
          break;
        case 3:
          var d = t.stateNode.containerInfo;
          d.nodeType === 1 ? d.textContent = "" : d.nodeType === 9 && d.documentElement && d.removeChild(d.documentElement);
          break;
        case 5:
        case 6:
        case 4:
        case 17:
          break;
        default:
          throw Error(j(163));
      }
    } catch (y) {
      b(t, t.return, y);
    }
    if (e = t.sibling, e !== null) {
      e.return = t.return, N = e;
      break;
    }
    N = t.return;
  }
  return _ = Fu, Fu = !1, _;
}
function Zn(e, t, n) {
  var r = t.updateQueue;
  if (r = r !== null ? r.lastEffect : null, r !== null) {
    var l = r = r.next;
    do {
      if ((l.tag & e) === e) {
        var o = l.destroy;
        l.destroy = void 0, o !== void 0 && mi(t, n, o);
      }
      l = l.next;
    } while (l !== r);
  }
}
function Al(e, t) {
  if (t = t.updateQueue, t = t !== null ? t.lastEffect : null, t !== null) {
    var n = t = t.next;
    do {
      if ((n.tag & e) === e) {
        var r = n.create;
        n.destroy = r();
      }
      n = n.next;
    } while (n !== t);
  }
}
function hi(e) {
  var t = e.ref;
  if (t !== null) {
    var n = e.stateNode;
    switch (e.tag) {
      case 5:
        e = n;
        break;
      default:
        e = n;
    }
    typeof t == "function" ? t(e) : t.current = e;
  }
}
function Qc(e) {
  var t = e.alternate;
  t !== null && (e.alternate = null, Qc(t)), e.child = null, e.deletions = null, e.sibling = null, e.tag === 5 && (t = e.stateNode, t !== null && (delete t[qe], delete t[cr], delete t[ti], delete t[ap], delete t[cp])), e.stateNode = null, e.return = null, e.dependencies = null, e.memoizedProps = null, e.memoizedState = null, e.pendingProps = null, e.stateNode = null, e.updateQueue = null;
}
function Kc(e) {
  return e.tag === 5 || e.tag === 3 || e.tag === 4;
}
function Nu(e) {
  e: for (; ; ) {
    for (; e.sibling === null; ) {
      if (e.return === null || Kc(e.return)) return null;
      e = e.return;
    }
    for (e.sibling.return = e.return, e = e.sibling; e.tag !== 5 && e.tag !== 6 && e.tag !== 18; ) {
      if (e.flags & 2 || e.child === null || e.tag === 4) continue e;
      e.child.return = e, e = e.child;
    }
    if (!(e.flags & 2)) return e.stateNode;
  }
}
function vi(e, t, n) {
  var r = e.tag;
  if (r === 5 || r === 6) e = e.stateNode, t ? n.nodeType === 8 ? n.parentNode.insertBefore(e, t) : n.insertBefore(e, t) : (n.nodeType === 8 ? (t = n.parentNode, t.insertBefore(e, n)) : (t = n, t.appendChild(e)), n = n._reactRootContainer, n != null || t.onclick !== null || (t.onclick = cl));
  else if (r !== 4 && (e = e.child, e !== null)) for (vi(e, t, n), e = e.sibling; e !== null; ) vi(e, t, n), e = e.sibling;
}
function gi(e, t, n) {
  var r = e.tag;
  if (r === 5 || r === 6) e = e.stateNode, t ? n.insertBefore(e, t) : n.appendChild(e);
  else if (r !== 4 && (e = e.child, e !== null)) for (gi(e, t, n), e = e.sibling; e !== null; ) gi(e, t, n), e = e.sibling;
}
var se = null, Be = !1;
function mt(e, t, n) {
  for (n = n.child; n !== null; ) Xc(e, t, n), n = n.sibling;
}
function Xc(e, t, n) {
  if (be && typeof be.onCommitFiberUnmount == "function") try {
    be.onCommitFiberUnmount(zl, n);
  } catch {
  }
  switch (n.tag) {
    case 5:
      he || dn(n, t);
    case 6:
      var r = se, l = Be;
      se = null, mt(e, t, n), se = r, Be = l, se !== null && (Be ? (e = se, n = n.stateNode, e.nodeType === 8 ? e.parentNode.removeChild(n) : e.removeChild(n)) : se.removeChild(n.stateNode));
      break;
    case 18:
      se !== null && (Be ? (e = se, n = n.stateNode, e.nodeType === 8 ? mo(e.parentNode, n) : e.nodeType === 1 && mo(e, n), or(e)) : mo(se, n.stateNode));
      break;
    case 4:
      r = se, l = Be, se = n.stateNode.containerInfo, Be = !0, mt(e, t, n), se = r, Be = l;
      break;
    case 0:
    case 11:
    case 14:
    case 15:
      if (!he && (r = n.updateQueue, r !== null && (r = r.lastEffect, r !== null))) {
        l = r = r.next;
        do {
          var o = l, i = o.destroy;
          o = o.tag, i !== void 0 && (o & 2 || o & 4) && mi(n, t, i), l = l.next;
        } while (l !== r);
      }
      mt(e, t, n);
      break;
    case 1:
      if (!he && (dn(n, t), r = n.stateNode, typeof r.componentWillUnmount == "function")) try {
        r.props = n.memoizedProps, r.state = n.memoizedState, r.componentWillUnmount();
      } catch (s) {
        b(n, t, s);
      }
      mt(e, t, n);
      break;
    case 21:
      mt(e, t, n);
      break;
    case 22:
      n.mode & 1 ? (he = (r = he) || n.memoizedState !== null, mt(e, t, n), he = r) : mt(e, t, n);
      break;
    default:
      mt(e, t, n);
  }
}
function Tu(e) {
  var t = e.updateQueue;
  if (t !== null) {
    e.updateQueue = null;
    var n = e.stateNode;
    n === null && (n = e.stateNode = new Ep()), t.forEach(function(r) {
      var l = Op.bind(null, e, r);
      n.has(r) || (n.add(r), r.then(l, l));
    });
  }
}
function $e(e, t) {
  var n = t.deletions;
  if (n !== null) for (var r = 0; r < n.length; r++) {
    var l = n[r];
    try {
      var o = e, i = t, s = i;
      e: for (; s !== null; ) {
        switch (s.tag) {
          case 5:
            se = s.stateNode, Be = !1;
            break e;
          case 3:
            se = s.stateNode.containerInfo, Be = !0;
            break e;
          case 4:
            se = s.stateNode.containerInfo, Be = !0;
            break e;
        }
        s = s.return;
      }
      if (se === null) throw Error(j(160));
      Xc(o, i, l), se = null, Be = !1;
      var u = l.alternate;
      u !== null && (u.return = null), l.return = null;
    } catch (a) {
      b(l, t, a);
    }
  }
  if (t.subtreeFlags & 12854) for (t = t.child; t !== null; ) Yc(t, e), t = t.sibling;
}
function Yc(e, t) {
  var n = e.alternate, r = e.flags;
  switch (e.tag) {
    case 0:
    case 11:
    case 14:
    case 15:
      if ($e(t, e), Je(e), r & 4) {
        try {
          Zn(3, e, e.return), Al(3, e);
        } catch (k) {
          b(e, e.return, k);
        }
        try {
          Zn(5, e, e.return);
        } catch (k) {
          b(e, e.return, k);
        }
      }
      break;
    case 1:
      $e(t, e), Je(e), r & 512 && n !== null && dn(n, n.return);
      break;
    case 5:
      if ($e(t, e), Je(e), r & 512 && n !== null && dn(n, n.return), e.flags & 32) {
        var l = e.stateNode;
        try {
          tr(l, "");
        } catch (k) {
          b(e, e.return, k);
        }
      }
      if (r & 4 && (l = e.stateNode, l != null)) {
        var o = e.memoizedProps, i = n !== null ? n.memoizedProps : o, s = e.type, u = e.updateQueue;
        if (e.updateQueue = null, u !== null) try {
          s === "input" && o.type === "radio" && o.name != null && va(l, o), $o(s, i);
          var a = $o(s, o);
          for (i = 0; i < u.length; i += 2) {
            var p = u[i], h = u[i + 1];
            p === "style" ? _a(l, h) : p === "dangerouslySetInnerHTML" ? xa(l, h) : p === "children" ? tr(l, h) : Ai(l, p, h, a);
          }
          switch (s) {
            case "input":
              Oo(l, o);
              break;
            case "textarea":
              ga(l, o);
              break;
            case "select":
              var m = l._wrapperState.wasMultiple;
              l._wrapperState.wasMultiple = !!o.multiple;
              var x = o.value;
              x != null ? mn(l, !!o.multiple, x, !1) : m !== !!o.multiple && (o.defaultValue != null ? mn(
                l,
                !!o.multiple,
                o.defaultValue,
                !0
              ) : mn(l, !!o.multiple, o.multiple ? [] : "", !1));
          }
          l[cr] = o;
        } catch (k) {
          b(e, e.return, k);
        }
      }
      break;
    case 6:
      if ($e(t, e), Je(e), r & 4) {
        if (e.stateNode === null) throw Error(j(162));
        l = e.stateNode, o = e.memoizedProps;
        try {
          l.nodeValue = o;
        } catch (k) {
          b(e, e.return, k);
        }
      }
      break;
    case 3:
      if ($e(t, e), Je(e), r & 4 && n !== null && n.memoizedState.isDehydrated) try {
        or(t.containerInfo);
      } catch (k) {
        b(e, e.return, k);
      }
      break;
    case 4:
      $e(t, e), Je(e);
      break;
    case 13:
      $e(t, e), Je(e), l = e.child, l.flags & 8192 && (o = l.memoizedState !== null, l.stateNode.isHidden = o, !o || l.alternate !== null && l.alternate.memoizedState !== null || (ys = ee())), r & 4 && Tu(e);
      break;
    case 22:
      if (p = n !== null && n.memoizedState !== null, e.mode & 1 ? (he = (a = he) || p, $e(t, e), he = a) : $e(t, e), Je(e), r & 8192) {
        if (a = e.memoizedState !== null, (e.stateNode.isHidden = a) && !p && e.mode & 1) for (N = e, p = e.child; p !== null; ) {
          for (h = N = p; N !== null; ) {
            switch (m = N, x = m.child, m.tag) {
              case 0:
              case 11:
              case 14:
              case 15:
                Zn(4, m, m.return);
                break;
              case 1:
                dn(m, m.return);
                var _ = m.stateNode;
                if (typeof _.componentWillUnmount == "function") {
                  r = m, n = m.return;
                  try {
                    t = r, _.props = t.memoizedProps, _.state = t.memoizedState, _.componentWillUnmount();
                  } catch (k) {
                    b(r, n, k);
                  }
                }
                break;
              case 5:
                dn(m, m.return);
                break;
              case 22:
                if (m.memoizedState !== null) {
                  zu(h);
                  continue;
                }
            }
            x !== null ? (x.return = m, N = x) : zu(h);
          }
          p = p.sibling;
        }
        e: for (p = null, h = e; ; ) {
          if (h.tag === 5) {
            if (p === null) {
              p = h;
              try {
                l = h.stateNode, a ? (o = l.style, typeof o.setProperty == "function" ? o.setProperty("display", "none", "important") : o.display = "none") : (s = h.stateNode, u = h.memoizedProps.style, i = u != null && u.hasOwnProperty("display") ? u.display : null, s.style.display = wa("display", i));
              } catch (k) {
                b(e, e.return, k);
              }
            }
          } else if (h.tag === 6) {
            if (p === null) try {
              h.stateNode.nodeValue = a ? "" : h.memoizedProps;
            } catch (k) {
              b(e, e.return, k);
            }
          } else if ((h.tag !== 22 && h.tag !== 23 || h.memoizedState === null || h === e) && h.child !== null) {
            h.child.return = h, h = h.child;
            continue;
          }
          if (h === e) break e;
          for (; h.sibling === null; ) {
            if (h.return === null || h.return === e) break e;
            p === h && (p = null), h = h.return;
          }
          p === h && (p = null), h.sibling.return = h.return, h = h.sibling;
        }
      }
      break;
    case 19:
      $e(t, e), Je(e), r & 4 && Tu(e);
      break;
    case 21:
      break;
    default:
      $e(
        t,
        e
      ), Je(e);
  }
}
function Je(e) {
  var t = e.flags;
  if (t & 2) {
    try {
      e: {
        for (var n = e.return; n !== null; ) {
          if (Kc(n)) {
            var r = n;
            break e;
          }
          n = n.return;
        }
        throw Error(j(160));
      }
      switch (r.tag) {
        case 5:
          var l = r.stateNode;
          r.flags & 32 && (tr(l, ""), r.flags &= -33);
          var o = Nu(e);
          gi(e, o, l);
          break;
        case 3:
        case 4:
          var i = r.stateNode.containerInfo, s = Nu(e);
          vi(e, s, i);
          break;
        default:
          throw Error(j(161));
      }
    } catch (u) {
      b(e, e.return, u);
    }
    e.flags &= -3;
  }
  t & 4096 && (e.flags &= -4097);
}
function Np(e, t, n) {
  N = e, Gc(e);
}
function Gc(e, t, n) {
  for (var r = (e.mode & 1) !== 0; N !== null; ) {
    var l = N, o = l.child;
    if (l.tag === 22 && r) {
      var i = l.memoizedState !== null || Ur;
      if (!i) {
        var s = l.alternate, u = s !== null && s.memoizedState !== null || he;
        s = Ur;
        var a = he;
        if (Ur = i, (he = u) && !a) for (N = l; N !== null; ) i = N, u = i.child, i.tag === 22 && i.memoizedState !== null ? Mu(l) : u !== null ? (u.return = i, N = u) : Mu(l);
        for (; o !== null; ) N = o, Gc(o), o = o.sibling;
        N = l, Ur = s, he = a;
      }
      Pu(e);
    } else l.subtreeFlags & 8772 && o !== null ? (o.return = l, N = o) : Pu(e);
  }
}
function Pu(e) {
  for (; N !== null; ) {
    var t = N;
    if (t.flags & 8772) {
      var n = t.alternate;
      try {
        if (t.flags & 8772) switch (t.tag) {
          case 0:
          case 11:
          case 15:
            he || Al(5, t);
            break;
          case 1:
            var r = t.stateNode;
            if (t.flags & 4 && !he) if (n === null) r.componentDidMount();
            else {
              var l = t.elementType === t.type ? n.memoizedProps : He(t.type, n.memoizedProps);
              r.componentDidUpdate(l, n.memoizedState, r.__reactInternalSnapshotBeforeUpdate);
            }
            var o = t.updateQueue;
            o !== null && mu(t, o, r);
            break;
          case 3:
            var i = t.updateQueue;
            if (i !== null) {
              if (n = null, t.child !== null) switch (t.child.tag) {
                case 5:
                  n = t.child.stateNode;
                  break;
                case 1:
                  n = t.child.stateNode;
              }
              mu(t, i, n);
            }
            break;
          case 5:
            var s = t.stateNode;
            if (n === null && t.flags & 4) {
              n = s;
              var u = t.memoizedProps;
              switch (t.type) {
                case "button":
                case "input":
                case "select":
                case "textarea":
                  u.autoFocus && n.focus();
                  break;
                case "img":
                  u.src && (n.src = u.src);
              }
            }
            break;
          case 6:
            break;
          case 4:
            break;
          case 12:
            break;
          case 13:
            if (t.memoizedState === null) {
              var a = t.alternate;
              if (a !== null) {
                var p = a.memoizedState;
                if (p !== null) {
                  var h = p.dehydrated;
                  h !== null && or(h);
                }
              }
            }
            break;
          case 19:
          case 17:
          case 21:
          case 22:
          case 23:
          case 25:
            break;
          default:
            throw Error(j(163));
        }
        he || t.flags & 512 && hi(t);
      } catch (m) {
        b(t, t.return, m);
      }
    }
    if (t === e) {
      N = null;
      break;
    }
    if (n = t.sibling, n !== null) {
      n.return = t.return, N = n;
      break;
    }
    N = t.return;
  }
}
function zu(e) {
  for (; N !== null; ) {
    var t = N;
    if (t === e) {
      N = null;
      break;
    }
    var n = t.sibling;
    if (n !== null) {
      n.return = t.return, N = n;
      break;
    }
    N = t.return;
  }
}
function Mu(e) {
  for (; N !== null; ) {
    var t = N;
    try {
      switch (t.tag) {
        case 0:
        case 11:
        case 15:
          var n = t.return;
          try {
            Al(4, t);
          } catch (u) {
            b(t, n, u);
          }
          break;
        case 1:
          var r = t.stateNode;
          if (typeof r.componentDidMount == "function") {
            var l = t.return;
            try {
              r.componentDidMount();
            } catch (u) {
              b(t, l, u);
            }
          }
          var o = t.return;
          try {
            hi(t);
          } catch (u) {
            b(t, o, u);
          }
          break;
        case 5:
          var i = t.return;
          try {
            hi(t);
          } catch (u) {
            b(t, i, u);
          }
      }
    } catch (u) {
      b(t, t.return, u);
    }
    if (t === e) {
      N = null;
      break;
    }
    var s = t.sibling;
    if (s !== null) {
      s.return = t.return, N = s;
      break;
    }
    N = t.return;
  }
}
var Tp = Math.ceil, _l = dt.ReactCurrentDispatcher, vs = dt.ReactCurrentOwner, We = dt.ReactCurrentBatchConfig, D = 0, ie = null, te = null, ue = 0, Ne = 0, pn = Mt(0), re = 0, vr = null, Kt = 0, Ul = 0, gs = 0, qn = null, je = null, ys = 0, En = 1 / 0, rt = null, jl = !1, yi = null, Et = null, $r = !1, wt = null, kl = 0, bn = 0, xi = null, br = -1, el = 0;
function ye() {
  return D & 6 ? ee() : br !== -1 ? br : br = ee();
}
function Ft(e) {
  return e.mode & 1 ? D & 2 && ue !== 0 ? ue & -ue : dp.transition !== null ? (el === 0 && (el = La()), el) : (e = A, e !== 0 || (e = window.event, e = e === void 0 ? 16 : Ua(e.type)), e) : 1;
}
function Xe(e, t, n, r) {
  if (50 < bn) throw bn = 0, xi = null, Error(j(185));
  _r(e, n, r), (!(D & 2) || e !== ie) && (e === ie && (!(D & 2) && (Ul |= n), re === 4 && yt(e, ue)), Ee(e, r), n === 1 && D === 0 && !(t.mode & 1) && (En = ee() + 500, Ol && Lt()));
}
function Ee(e, t) {
  var n = e.callbackNode;
  dd(e, t);
  var r = il(e, e === ie ? ue : 0);
  if (r === 0) n !== null && $s(n), e.callbackNode = null, e.callbackPriority = 0;
  else if (t = r & -r, e.callbackPriority !== t) {
    if (n != null && $s(n), t === 1) e.tag === 0 ? fp(Lu.bind(null, e)) : oc(Lu.bind(null, e)), sp(function() {
      !(D & 6) && Lt();
    }), n = null;
    else {
      switch (Ra(r)) {
        case 1:
          n = Vi;
          break;
        case 4:
          n = za;
          break;
        case 16:
          n = ol;
          break;
        case 536870912:
          n = Ma;
          break;
        default:
          n = ol;
      }
      n = rf(n, Jc.bind(null, e));
    }
    e.callbackPriority = t, e.callbackNode = n;
  }
}
function Jc(e, t) {
  if (br = -1, el = 0, D & 6) throw Error(j(327));
  var n = e.callbackNode;
  if (xn() && e.callbackNode !== n) return null;
  var r = il(e, e === ie ? ue : 0);
  if (r === 0) return null;
  if (r & 30 || r & e.expiredLanes || t) t = Sl(e, r);
  else {
    t = r;
    var l = D;
    D |= 2;
    var o = qc();
    (ie !== e || ue !== t) && (rt = null, En = ee() + 500, $t(e, t));
    do
      try {
        Mp();
        break;
      } catch (s) {
        Zc(e, s);
      }
    while (!0);
    rs(), _l.current = o, D = l, te !== null ? t = 0 : (ie = null, ue = 0, t = re);
  }
  if (t !== 0) {
    if (t === 2 && (l = Ko(e), l !== 0 && (r = l, t = wi(e, l))), t === 1) throw n = vr, $t(e, 0), yt(e, r), Ee(e, ee()), n;
    if (t === 6) yt(e, r);
    else {
      if (l = e.current.alternate, !(r & 30) && !Pp(l) && (t = Sl(e, r), t === 2 && (o = Ko(e), o !== 0 && (r = o, t = wi(e, o))), t === 1)) throw n = vr, $t(e, 0), yt(e, r), Ee(e, ee()), n;
      switch (e.finishedWork = l, e.finishedLanes = r, t) {
        case 0:
        case 1:
          throw Error(j(345));
        case 2:
          Ot(e, je, rt);
          break;
        case 3:
          if (yt(e, r), (r & 130023424) === r && (t = ys + 500 - ee(), 10 < t)) {
            if (il(e, 0) !== 0) break;
            if (l = e.suspendedLanes, (l & r) !== r) {
              ye(), e.pingedLanes |= e.suspendedLanes & l;
              break;
            }
            e.timeoutHandle = ei(Ot.bind(null, e, je, rt), t);
            break;
          }
          Ot(e, je, rt);
          break;
        case 4:
          if (yt(e, r), (r & 4194240) === r) break;
          for (t = e.eventTimes, l = -1; 0 < r; ) {
            var i = 31 - Ke(r);
            o = 1 << i, i = t[i], i > l && (l = i), r &= ~o;
          }
          if (r = l, r = ee() - r, r = (120 > r ? 120 : 480 > r ? 480 : 1080 > r ? 1080 : 1920 > r ? 1920 : 3e3 > r ? 3e3 : 4320 > r ? 4320 : 1960 * Tp(r / 1960)) - r, 10 < r) {
            e.timeoutHandle = ei(Ot.bind(null, e, je, rt), r);
            break;
          }
          Ot(e, je, rt);
          break;
        case 5:
          Ot(e, je, rt);
          break;
        default:
          throw Error(j(329));
      }
    }
  }
  return Ee(e, ee()), e.callbackNode === n ? Jc.bind(null, e) : null;
}
function wi(e, t) {
  var n = qn;
  return e.current.memoizedState.isDehydrated && ($t(e, t).flags |= 256), e = Sl(e, t), e !== 2 && (t = je, je = n, t !== null && _i(t)), e;
}
function _i(e) {
  je === null ? je = e : je.push.apply(je, e);
}
function Pp(e) {
  for (var t = e; ; ) {
    if (t.flags & 16384) {
      var n = t.updateQueue;
      if (n !== null && (n = n.stores, n !== null)) for (var r = 0; r < n.length; r++) {
        var l = n[r], o = l.getSnapshot;
        l = l.value;
        try {
          if (!Ye(o(), l)) return !1;
        } catch {
          return !1;
        }
      }
    }
    if (n = t.child, t.subtreeFlags & 16384 && n !== null) n.return = t, t = n;
    else {
      if (t === e) break;
      for (; t.sibling === null; ) {
        if (t.return === null || t.return === e) return !0;
        t = t.return;
      }
      t.sibling.return = t.return, t = t.sibling;
    }
  }
  return !0;
}
function yt(e, t) {
  for (t &= ~gs, t &= ~Ul, e.suspendedLanes |= t, e.pingedLanes &= ~t, e = e.expirationTimes; 0 < t; ) {
    var n = 31 - Ke(t), r = 1 << n;
    e[n] = -1, t &= ~r;
  }
}
function Lu(e) {
  if (D & 6) throw Error(j(327));
  xn();
  var t = il(e, 0);
  if (!(t & 1)) return Ee(e, ee()), null;
  var n = Sl(e, t);
  if (e.tag !== 0 && n === 2) {
    var r = Ko(e);
    r !== 0 && (t = r, n = wi(e, r));
  }
  if (n === 1) throw n = vr, $t(e, 0), yt(e, t), Ee(e, ee()), n;
  if (n === 6) throw Error(j(345));
  return e.finishedWork = e.current.alternate, e.finishedLanes = t, Ot(e, je, rt), Ee(e, ee()), null;
}
function xs(e, t) {
  var n = D;
  D |= 1;
  try {
    return e(t);
  } finally {
    D = n, D === 0 && (En = ee() + 500, Ol && Lt());
  }
}
function Xt(e) {
  wt !== null && wt.tag === 0 && !(D & 6) && xn();
  var t = D;
  D |= 1;
  var n = We.transition, r = A;
  try {
    if (We.transition = null, A = 1, e) return e();
  } finally {
    A = r, We.transition = n, D = t, !(D & 6) && Lt();
  }
}
function ws() {
  Ne = pn.current, K(pn);
}
function $t(e, t) {
  e.finishedWork = null, e.finishedLanes = 0;
  var n = e.timeoutHandle;
  if (n !== -1 && (e.timeoutHandle = -1, ip(n)), te !== null) for (n = te.return; n !== null; ) {
    var r = n;
    switch (es(r), r.tag) {
      case 1:
        r = r.type.childContextTypes, r != null && fl();
        break;
      case 3:
        Sn(), K(Se), K(ve), as();
        break;
      case 5:
        us(r);
        break;
      case 4:
        Sn();
        break;
      case 13:
        K(Y);
        break;
      case 19:
        K(Y);
        break;
      case 10:
        ls(r.type._context);
        break;
      case 22:
      case 23:
        ws();
    }
    n = n.return;
  }
  if (ie = e, te = e = Nt(e.current, null), ue = Ne = t, re = 0, vr = null, gs = Ul = Kt = 0, je = qn = null, At !== null) {
    for (t = 0; t < At.length; t++) if (n = At[t], r = n.interleaved, r !== null) {
      n.interleaved = null;
      var l = r.next, o = n.pending;
      if (o !== null) {
        var i = o.next;
        o.next = l, r.next = i;
      }
      n.pending = r;
    }
    At = null;
  }
  return e;
}
function Zc(e, t) {
  do {
    var n = te;
    try {
      if (rs(), Jr.current = wl, xl) {
        for (var r = G.memoizedState; r !== null; ) {
          var l = r.queue;
          l !== null && (l.pending = null), r = r.next;
        }
        xl = !1;
      }
      if (Qt = 0, oe = ne = G = null, Jn = !1, pr = 0, vs.current = null, n === null || n.return === null) {
        re = 1, vr = t, te = null;
        break;
      }
      e: {
        var o = e, i = n.return, s = n, u = t;
        if (t = ue, s.flags |= 32768, u !== null && typeof u == "object" && typeof u.then == "function") {
          var a = u, p = s, h = p.tag;
          if (!(p.mode & 1) && (h === 0 || h === 11 || h === 15)) {
            var m = p.alternate;
            m ? (p.updateQueue = m.updateQueue, p.memoizedState = m.memoizedState, p.lanes = m.lanes) : (p.updateQueue = null, p.memoizedState = null);
          }
          var x = wu(i);
          if (x !== null) {
            x.flags &= -257, _u(x, i, s, o, t), x.mode & 1 && xu(o, a, t), t = x, u = a;
            var _ = t.updateQueue;
            if (_ === null) {
              var k = /* @__PURE__ */ new Set();
              k.add(u), t.updateQueue = k;
            } else _.add(u);
            break e;
          } else {
            if (!(t & 1)) {
              xu(o, a, t), _s();
              break e;
            }
            u = Error(j(426));
          }
        } else if (X && s.mode & 1) {
          var I = wu(i);
          if (I !== null) {
            !(I.flags & 65536) && (I.flags |= 256), _u(I, i, s, o, t), ts(Cn(u, s));
            break e;
          }
        }
        o = u = Cn(u, s), re !== 4 && (re = 2), qn === null ? qn = [o] : qn.push(o), o = i;
        do {
          switch (o.tag) {
            case 3:
              o.flags |= 65536, t &= -t, o.lanes |= t;
              var f = Rc(o, u, t);
              pu(o, f);
              break e;
            case 1:
              s = u;
              var c = o.type, d = o.stateNode;
              if (!(o.flags & 128) && (typeof c.getDerivedStateFromError == "function" || d !== null && typeof d.componentDidCatch == "function" && (Et === null || !Et.has(d)))) {
                o.flags |= 65536, t &= -t, o.lanes |= t;
                var y = Ic(o, s, t);
                pu(o, y);
                break e;
              }
          }
          o = o.return;
        } while (o !== null);
      }
      ef(n);
    } catch (S) {
      t = S, te === n && n !== null && (te = n = n.return);
      continue;
    }
    break;
  } while (!0);
}
function qc() {
  var e = _l.current;
  return _l.current = wl, e === null ? wl : e;
}
function _s() {
  (re === 0 || re === 3 || re === 2) && (re = 4), ie === null || !(Kt & 268435455) && !(Ul & 268435455) || yt(ie, ue);
}
function Sl(e, t) {
  var n = D;
  D |= 2;
  var r = qc();
  (ie !== e || ue !== t) && (rt = null, $t(e, t));
  do
    try {
      zp();
      break;
    } catch (l) {
      Zc(e, l);
    }
  while (!0);
  if (rs(), D = n, _l.current = r, te !== null) throw Error(j(261));
  return ie = null, ue = 0, re;
}
function zp() {
  for (; te !== null; ) bc(te);
}
function Mp() {
  for (; te !== null && !rd(); ) bc(te);
}
function bc(e) {
  var t = nf(e.alternate, e, Ne);
  e.memoizedProps = e.pendingProps, t === null ? ef(e) : te = t, vs.current = null;
}
function ef(e) {
  var t = e;
  do {
    var n = t.alternate;
    if (e = t.return, t.flags & 32768) {
      if (n = Cp(n, t), n !== null) {
        n.flags &= 32767, te = n;
        return;
      }
      if (e !== null) e.flags |= 32768, e.subtreeFlags = 0, e.deletions = null;
      else {
        re = 6, te = null;
        return;
      }
    } else if (n = Sp(n, t, Ne), n !== null) {
      te = n;
      return;
    }
    if (t = t.sibling, t !== null) {
      te = t;
      return;
    }
    te = t = e;
  } while (t !== null);
  re === 0 && (re = 5);
}
function Ot(e, t, n) {
  var r = A, l = We.transition;
  try {
    We.transition = null, A = 1, Lp(e, t, n, r);
  } finally {
    We.transition = l, A = r;
  }
  return null;
}
function Lp(e, t, n, r) {
  do
    xn();
  while (wt !== null);
  if (D & 6) throw Error(j(327));
  n = e.finishedWork;
  var l = e.finishedLanes;
  if (n === null) return null;
  if (e.finishedWork = null, e.finishedLanes = 0, n === e.current) throw Error(j(177));
  e.callbackNode = null, e.callbackPriority = 0;
  var o = n.lanes | n.childLanes;
  if (pd(e, o), e === ie && (te = ie = null, ue = 0), !(n.subtreeFlags & 2064) && !(n.flags & 2064) || $r || ($r = !0, rf(ol, function() {
    return xn(), null;
  })), o = (n.flags & 15990) !== 0, n.subtreeFlags & 15990 || o) {
    o = We.transition, We.transition = null;
    var i = A;
    A = 1;
    var s = D;
    D |= 4, vs.current = null, Fp(e, n), Yc(n, e), bd(qo), sl = !!Zo, qo = Zo = null, e.current = n, Np(n), ld(), D = s, A = i, We.transition = o;
  } else e.current = n;
  if ($r && ($r = !1, wt = e, kl = l), o = e.pendingLanes, o === 0 && (Et = null), sd(n.stateNode), Ee(e, ee()), t !== null) for (r = e.onRecoverableError, n = 0; n < t.length; n++) l = t[n], r(l.value, { componentStack: l.stack, digest: l.digest });
  if (jl) throw jl = !1, e = yi, yi = null, e;
  return kl & 1 && e.tag !== 0 && xn(), o = e.pendingLanes, o & 1 ? e === xi ? bn++ : (bn = 0, xi = e) : bn = 0, Lt(), null;
}
function xn() {
  if (wt !== null) {
    var e = Ra(kl), t = We.transition, n = A;
    try {
      if (We.transition = null, A = 16 > e ? 16 : e, wt === null) var r = !1;
      else {
        if (e = wt, wt = null, kl = 0, D & 6) throw Error(j(331));
        var l = D;
        for (D |= 4, N = e.current; N !== null; ) {
          var o = N, i = o.child;
          if (N.flags & 16) {
            var s = o.deletions;
            if (s !== null) {
              for (var u = 0; u < s.length; u++) {
                var a = s[u];
                for (N = a; N !== null; ) {
                  var p = N;
                  switch (p.tag) {
                    case 0:
                    case 11:
                    case 15:
                      Zn(8, p, o);
                  }
                  var h = p.child;
                  if (h !== null) h.return = p, N = h;
                  else for (; N !== null; ) {
                    p = N;
                    var m = p.sibling, x = p.return;
                    if (Qc(p), p === a) {
                      N = null;
                      break;
                    }
                    if (m !== null) {
                      m.return = x, N = m;
                      break;
                    }
                    N = x;
                  }
                }
              }
              var _ = o.alternate;
              if (_ !== null) {
                var k = _.child;
                if (k !== null) {
                  _.child = null;
                  do {
                    var I = k.sibling;
                    k.sibling = null, k = I;
                  } while (k !== null);
                }
              }
              N = o;
            }
          }
          if (o.subtreeFlags & 2064 && i !== null) i.return = o, N = i;
          else e: for (; N !== null; ) {
            if (o = N, o.flags & 2048) switch (o.tag) {
              case 0:
              case 11:
              case 15:
                Zn(9, o, o.return);
            }
            var f = o.sibling;
            if (f !== null) {
              f.return = o.return, N = f;
              break e;
            }
            N = o.return;
          }
        }
        var c = e.current;
        for (N = c; N !== null; ) {
          i = N;
          var d = i.child;
          if (i.subtreeFlags & 2064 && d !== null) d.return = i, N = d;
          else e: for (i = c; N !== null; ) {
            if (s = N, s.flags & 2048) try {
              switch (s.tag) {
                case 0:
                case 11:
                case 15:
                  Al(9, s);
              }
            } catch (S) {
              b(s, s.return, S);
            }
            if (s === i) {
              N = null;
              break e;
            }
            var y = s.sibling;
            if (y !== null) {
              y.return = s.return, N = y;
              break e;
            }
            N = s.return;
          }
        }
        if (D = l, Lt(), be && typeof be.onPostCommitFiberRoot == "function") try {
          be.onPostCommitFiberRoot(zl, e);
        } catch {
        }
        r = !0;
      }
      return r;
    } finally {
      A = n, We.transition = t;
    }
  }
  return !1;
}
function Ru(e, t, n) {
  t = Cn(n, t), t = Rc(e, t, 1), e = Ct(e, t, 1), t = ye(), e !== null && (_r(e, 1, t), Ee(e, t));
}
function b(e, t, n) {
  if (e.tag === 3) Ru(e, e, n);
  else for (; t !== null; ) {
    if (t.tag === 3) {
      Ru(t, e, n);
      break;
    } else if (t.tag === 1) {
      var r = t.stateNode;
      if (typeof t.type.getDerivedStateFromError == "function" || typeof r.componentDidCatch == "function" && (Et === null || !Et.has(r))) {
        e = Cn(n, e), e = Ic(t, e, 1), t = Ct(t, e, 1), e = ye(), t !== null && (_r(t, 1, e), Ee(t, e));
        break;
      }
    }
    t = t.return;
  }
}
function Rp(e, t, n) {
  var r = e.pingCache;
  r !== null && r.delete(t), t = ye(), e.pingedLanes |= e.suspendedLanes & n, ie === e && (ue & n) === n && (re === 4 || re === 3 && (ue & 130023424) === ue && 500 > ee() - ys ? $t(e, 0) : gs |= n), Ee(e, t);
}
function tf(e, t) {
  t === 0 && (e.mode & 1 ? (t = zr, zr <<= 1, !(zr & 130023424) && (zr = 4194304)) : t = 1);
  var n = ye();
  e = ct(e, t), e !== null && (_r(e, t, n), Ee(e, n));
}
function Ip(e) {
  var t = e.memoizedState, n = 0;
  t !== null && (n = t.retryLane), tf(e, n);
}
function Op(e, t) {
  var n = 0;
  switch (e.tag) {
    case 13:
      var r = e.stateNode, l = e.memoizedState;
      l !== null && (n = l.retryLane);
      break;
    case 19:
      r = e.stateNode;
      break;
    default:
      throw Error(j(314));
  }
  r !== null && r.delete(t), tf(e, n);
}
var nf;
nf = function(e, t, n) {
  if (e !== null) if (e.memoizedProps !== t.pendingProps || Se.current) ke = !0;
  else {
    if (!(e.lanes & n) && !(t.flags & 128)) return ke = !1, kp(e, t, n);
    ke = !!(e.flags & 131072);
  }
  else ke = !1, X && t.flags & 1048576 && ic(t, ml, t.index);
  switch (t.lanes = 0, t.tag) {
    case 2:
      var r = t.type;
      qr(e, t), e = t.pendingProps;
      var l = _n(t, ve.current);
      yn(t, n), l = fs(null, t, r, e, l, n);
      var o = ds();
      return t.flags |= 1, typeof l == "object" && l !== null && typeof l.render == "function" && l.$$typeof === void 0 ? (t.tag = 1, t.memoizedState = null, t.updateQueue = null, Ce(r) ? (o = !0, dl(t)) : o = !1, t.memoizedState = l.state !== null && l.state !== void 0 ? l.state : null, is(t), l.updater = Wl, t.stateNode = l, l._reactInternals = t, si(t, r, e, n), t = ci(null, t, r, !0, o, n)) : (t.tag = 0, X && o && bi(t), ge(null, t, l, n), t = t.child), t;
    case 16:
      r = t.elementType;
      e: {
        switch (qr(e, t), e = t.pendingProps, l = r._init, r = l(r._payload), t.type = r, l = t.tag = Wp(r), e = He(r, e), l) {
          case 0:
            t = ai(null, t, r, e, n);
            break e;
          case 1:
            t = Su(null, t, r, e, n);
            break e;
          case 11:
            t = ju(null, t, r, e, n);
            break e;
          case 14:
            t = ku(null, t, r, He(r.type, e), n);
            break e;
        }
        throw Error(j(
          306,
          r,
          ""
        ));
      }
      return t;
    case 0:
      return r = t.type, l = t.pendingProps, l = t.elementType === r ? l : He(r, l), ai(e, t, r, l, n);
    case 1:
      return r = t.type, l = t.pendingProps, l = t.elementType === r ? l : He(r, l), Su(e, t, r, l, n);
    case 3:
      e: {
        if (Ac(t), e === null) throw Error(j(387));
        r = t.pendingProps, o = t.memoizedState, l = o.element, dc(e, t), gl(t, r, null, n);
        var i = t.memoizedState;
        if (r = i.element, o.isDehydrated) if (o = { element: r, isDehydrated: !1, cache: i.cache, pendingSuspenseBoundaries: i.pendingSuspenseBoundaries, transitions: i.transitions }, t.updateQueue.baseState = o, t.memoizedState = o, t.flags & 256) {
          l = Cn(Error(j(423)), t), t = Cu(e, t, r, n, l);
          break e;
        } else if (r !== l) {
          l = Cn(Error(j(424)), t), t = Cu(e, t, r, n, l);
          break e;
        } else for (Te = St(t.stateNode.containerInfo.firstChild), Pe = t, X = !0, Ve = null, n = cc(t, null, r, n), t.child = n; n; ) n.flags = n.flags & -3 | 4096, n = n.sibling;
        else {
          if (jn(), r === l) {
            t = ft(e, t, n);
            break e;
          }
          ge(e, t, r, n);
        }
        t = t.child;
      }
      return t;
    case 5:
      return pc(t), e === null && li(t), r = t.type, l = t.pendingProps, o = e !== null ? e.memoizedProps : null, i = l.children, bo(r, l) ? i = null : o !== null && bo(r, o) && (t.flags |= 32), Wc(e, t), ge(e, t, i, n), t.child;
    case 6:
      return e === null && li(t), null;
    case 13:
      return Uc(e, t, n);
    case 4:
      return ss(t, t.stateNode.containerInfo), r = t.pendingProps, e === null ? t.child = kn(t, null, r, n) : ge(e, t, r, n), t.child;
    case 11:
      return r = t.type, l = t.pendingProps, l = t.elementType === r ? l : He(r, l), ju(e, t, r, l, n);
    case 7:
      return ge(e, t, t.pendingProps, n), t.child;
    case 8:
      return ge(e, t, t.pendingProps.children, n), t.child;
    case 12:
      return ge(e, t, t.pendingProps.children, n), t.child;
    case 10:
      e: {
        if (r = t.type._context, l = t.pendingProps, o = t.memoizedProps, i = l.value, B(hl, r._currentValue), r._currentValue = i, o !== null) if (Ye(o.value, i)) {
          if (o.children === l.children && !Se.current) {
            t = ft(e, t, n);
            break e;
          }
        } else for (o = t.child, o !== null && (o.return = t); o !== null; ) {
          var s = o.dependencies;
          if (s !== null) {
            i = o.child;
            for (var u = s.firstContext; u !== null; ) {
              if (u.context === r) {
                if (o.tag === 1) {
                  u = st(-1, n & -n), u.tag = 2;
                  var a = o.updateQueue;
                  if (a !== null) {
                    a = a.shared;
                    var p = a.pending;
                    p === null ? u.next = u : (u.next = p.next, p.next = u), a.pending = u;
                  }
                }
                o.lanes |= n, u = o.alternate, u !== null && (u.lanes |= n), oi(
                  o.return,
                  n,
                  t
                ), s.lanes |= n;
                break;
              }
              u = u.next;
            }
          } else if (o.tag === 10) i = o.type === t.type ? null : o.child;
          else if (o.tag === 18) {
            if (i = o.return, i === null) throw Error(j(341));
            i.lanes |= n, s = i.alternate, s !== null && (s.lanes |= n), oi(i, n, t), i = o.sibling;
          } else i = o.child;
          if (i !== null) i.return = o;
          else for (i = o; i !== null; ) {
            if (i === t) {
              i = null;
              break;
            }
            if (o = i.sibling, o !== null) {
              o.return = i.return, i = o;
              break;
            }
            i = i.return;
          }
          o = i;
        }
        ge(e, t, l.children, n), t = t.child;
      }
      return t;
    case 9:
      return l = t.type, r = t.pendingProps.children, yn(t, n), l = Ae(l), r = r(l), t.flags |= 1, ge(e, t, r, n), t.child;
    case 14:
      return r = t.type, l = He(r, t.pendingProps), l = He(r.type, l), ku(e, t, r, l, n);
    case 15:
      return Oc(e, t, t.type, t.pendingProps, n);
    case 17:
      return r = t.type, l = t.pendingProps, l = t.elementType === r ? l : He(r, l), qr(e, t), t.tag = 1, Ce(r) ? (e = !0, dl(t)) : e = !1, yn(t, n), Lc(t, r, l), si(t, r, l, n), ci(null, t, r, !0, e, n);
    case 19:
      return $c(e, t, n);
    case 22:
      return Dc(e, t, n);
  }
  throw Error(j(156, t.tag));
};
function rf(e, t) {
  return Pa(e, t);
}
function Dp(e, t, n, r) {
  this.tag = e, this.key = n, this.sibling = this.child = this.return = this.stateNode = this.type = this.elementType = null, this.index = 0, this.ref = null, this.pendingProps = t, this.dependencies = this.memoizedState = this.updateQueue = this.memoizedProps = null, this.mode = r, this.subtreeFlags = this.flags = 0, this.deletions = null, this.childLanes = this.lanes = 0, this.alternate = null;
}
function De(e, t, n, r) {
  return new Dp(e, t, n, r);
}
function js(e) {
  return e = e.prototype, !(!e || !e.isReactComponent);
}
function Wp(e) {
  if (typeof e == "function") return js(e) ? 1 : 0;
  if (e != null) {
    if (e = e.$$typeof, e === $i) return 11;
    if (e === Hi) return 14;
  }
  return 2;
}
function Nt(e, t) {
  var n = e.alternate;
  return n === null ? (n = De(e.tag, t, e.key, e.mode), n.elementType = e.elementType, n.type = e.type, n.stateNode = e.stateNode, n.alternate = e, e.alternate = n) : (n.pendingProps = t, n.type = e.type, n.flags = 0, n.subtreeFlags = 0, n.deletions = null), n.flags = e.flags & 14680064, n.childLanes = e.childLanes, n.lanes = e.lanes, n.child = e.child, n.memoizedProps = e.memoizedProps, n.memoizedState = e.memoizedState, n.updateQueue = e.updateQueue, t = e.dependencies, n.dependencies = t === null ? null : { lanes: t.lanes, firstContext: t.firstContext }, n.sibling = e.sibling, n.index = e.index, n.ref = e.ref, n;
}
function tl(e, t, n, r, l, o) {
  var i = 2;
  if (r = e, typeof e == "function") js(e) && (i = 1);
  else if (typeof e == "string") i = 5;
  else e: switch (e) {
    case nn:
      return Ht(n.children, l, o, t);
    case Ui:
      i = 8, l |= 8;
      break;
    case zo:
      return e = De(12, n, t, l | 2), e.elementType = zo, e.lanes = o, e;
    case Mo:
      return e = De(13, n, t, l), e.elementType = Mo, e.lanes = o, e;
    case Lo:
      return e = De(19, n, t, l), e.elementType = Lo, e.lanes = o, e;
    case pa:
      return $l(n, l, o, t);
    default:
      if (typeof e == "object" && e !== null) switch (e.$$typeof) {
        case fa:
          i = 10;
          break e;
        case da:
          i = 9;
          break e;
        case $i:
          i = 11;
          break e;
        case Hi:
          i = 14;
          break e;
        case ht:
          i = 16, r = null;
          break e;
      }
      throw Error(j(130, e == null ? e : typeof e, ""));
  }
  return t = De(i, n, t, l), t.elementType = e, t.type = r, t.lanes = o, t;
}
function Ht(e, t, n, r) {
  return e = De(7, e, r, t), e.lanes = n, e;
}
function $l(e, t, n, r) {
  return e = De(22, e, r, t), e.elementType = pa, e.lanes = n, e.stateNode = { isHidden: !1 }, e;
}
function jo(e, t, n) {
  return e = De(6, e, null, t), e.lanes = n, e;
}
function ko(e, t, n) {
  return t = De(4, e.children !== null ? e.children : [], e.key, t), t.lanes = n, t.stateNode = { containerInfo: e.containerInfo, pendingChildren: null, implementation: e.implementation }, t;
}
function Ap(e, t, n, r, l) {
  this.tag = t, this.containerInfo = e, this.finishedWork = this.pingCache = this.current = this.pendingChildren = null, this.timeoutHandle = -1, this.callbackNode = this.pendingContext = this.context = null, this.callbackPriority = 0, this.eventTimes = no(0), this.expirationTimes = no(-1), this.entangledLanes = this.finishedLanes = this.mutableReadLanes = this.expiredLanes = this.pingedLanes = this.suspendedLanes = this.pendingLanes = 0, this.entanglements = no(0), this.identifierPrefix = r, this.onRecoverableError = l, this.mutableSourceEagerHydrationData = null;
}
function ks(e, t, n, r, l, o, i, s, u) {
  return e = new Ap(e, t, n, s, u), t === 1 ? (t = 1, o === !0 && (t |= 8)) : t = 0, o = De(3, null, null, t), e.current = o, o.stateNode = e, o.memoizedState = { element: r, isDehydrated: n, cache: null, transitions: null, pendingSuspenseBoundaries: null }, is(o), e;
}
function Up(e, t, n) {
  var r = 3 < arguments.length && arguments[3] !== void 0 ? arguments[3] : null;
  return { $$typeof: tn, key: r == null ? null : "" + r, children: e, containerInfo: t, implementation: n };
}
function lf(e) {
  if (!e) return Pt;
  e = e._reactInternals;
  e: {
    if (Zt(e) !== e || e.tag !== 1) throw Error(j(170));
    var t = e;
    do {
      switch (t.tag) {
        case 3:
          t = t.stateNode.context;
          break e;
        case 1:
          if (Ce(t.type)) {
            t = t.stateNode.__reactInternalMemoizedMergedChildContext;
            break e;
          }
      }
      t = t.return;
    } while (t !== null);
    throw Error(j(171));
  }
  if (e.tag === 1) {
    var n = e.type;
    if (Ce(n)) return lc(e, n, t);
  }
  return t;
}
function of(e, t, n, r, l, o, i, s, u) {
  return e = ks(n, r, !0, e, l, o, i, s, u), e.context = lf(null), n = e.current, r = ye(), l = Ft(n), o = st(r, l), o.callback = t ?? null, Ct(n, o, l), e.current.lanes = l, _r(e, l, r), Ee(e, r), e;
}
function Hl(e, t, n, r) {
  var l = t.current, o = ye(), i = Ft(l);
  return n = lf(n), t.context === null ? t.context = n : t.pendingContext = n, t = st(o, i), t.payload = { element: e }, r = r === void 0 ? null : r, r !== null && (t.callback = r), e = Ct(l, t, i), e !== null && (Xe(e, l, i, o), Gr(e, l, i)), i;
}
function Cl(e) {
  if (e = e.current, !e.child) return null;
  switch (e.child.tag) {
    case 5:
      return e.child.stateNode;
    default:
      return e.child.stateNode;
  }
}
function Iu(e, t) {
  if (e = e.memoizedState, e !== null && e.dehydrated !== null) {
    var n = e.retryLane;
    e.retryLane = n !== 0 && n < t ? n : t;
  }
}
function Ss(e, t) {
  Iu(e, t), (e = e.alternate) && Iu(e, t);
}
function $p() {
  return null;
}
var sf = typeof reportError == "function" ? reportError : function(e) {
  console.error(e);
};
function Cs(e) {
  this._internalRoot = e;
}
Bl.prototype.render = Cs.prototype.render = function(e) {
  var t = this._internalRoot;
  if (t === null) throw Error(j(409));
  Hl(e, t, null, null);
};
Bl.prototype.unmount = Cs.prototype.unmount = function() {
  var e = this._internalRoot;
  if (e !== null) {
    this._internalRoot = null;
    var t = e.containerInfo;
    Xt(function() {
      Hl(null, e, null, null);
    }), t[at] = null;
  }
};
function Bl(e) {
  this._internalRoot = e;
}
Bl.prototype.unstable_scheduleHydration = function(e) {
  if (e) {
    var t = Da();
    e = { blockedOn: null, target: e, priority: t };
    for (var n = 0; n < gt.length && t !== 0 && t < gt[n].priority; n++) ;
    gt.splice(n, 0, e), n === 0 && Aa(e);
  }
};
function Es(e) {
  return !(!e || e.nodeType !== 1 && e.nodeType !== 9 && e.nodeType !== 11);
}
function Vl(e) {
  return !(!e || e.nodeType !== 1 && e.nodeType !== 9 && e.nodeType !== 11 && (e.nodeType !== 8 || e.nodeValue !== " react-mount-point-unstable "));
}
function Ou() {
}
function Hp(e, t, n, r, l) {
  if (l) {
    if (typeof r == "function") {
      var o = r;
      r = function() {
        var a = Cl(i);
        o.call(a);
      };
    }
    var i = of(t, r, e, 0, null, !1, !1, "", Ou);
    return e._reactRootContainer = i, e[at] = i.current, ur(e.nodeType === 8 ? e.parentNode : e), Xt(), i;
  }
  for (; l = e.lastChild; ) e.removeChild(l);
  if (typeof r == "function") {
    var s = r;
    r = function() {
      var a = Cl(u);
      s.call(a);
    };
  }
  var u = ks(e, 0, !1, null, null, !1, !1, "", Ou);
  return e._reactRootContainer = u, e[at] = u.current, ur(e.nodeType === 8 ? e.parentNode : e), Xt(function() {
    Hl(t, u, n, r);
  }), u;
}
function Ql(e, t, n, r, l) {
  var o = n._reactRootContainer;
  if (o) {
    var i = o;
    if (typeof l == "function") {
      var s = l;
      l = function() {
        var u = Cl(i);
        s.call(u);
      };
    }
    Hl(t, i, e, l);
  } else i = Hp(n, t, e, l, r);
  return Cl(i);
}
Ia = function(e) {
  switch (e.tag) {
    case 3:
      var t = e.stateNode;
      if (t.current.memoizedState.isDehydrated) {
        var n = Bn(t.pendingLanes);
        n !== 0 && (Qi(t, n | 1), Ee(t, ee()), !(D & 6) && (En = ee() + 500, Lt()));
      }
      break;
    case 13:
      Xt(function() {
        var r = ct(e, 1);
        if (r !== null) {
          var l = ye();
          Xe(r, e, 1, l);
        }
      }), Ss(e, 1);
  }
};
Ki = function(e) {
  if (e.tag === 13) {
    var t = ct(e, 134217728);
    if (t !== null) {
      var n = ye();
      Xe(t, e, 134217728, n);
    }
    Ss(e, 134217728);
  }
};
Oa = function(e) {
  if (e.tag === 13) {
    var t = Ft(e), n = ct(e, t);
    if (n !== null) {
      var r = ye();
      Xe(n, e, t, r);
    }
    Ss(e, t);
  }
};
Da = function() {
  return A;
};
Wa = function(e, t) {
  var n = A;
  try {
    return A = e, t();
  } finally {
    A = n;
  }
};
Bo = function(e, t, n) {
  switch (t) {
    case "input":
      if (Oo(e, n), t = n.name, n.type === "radio" && t != null) {
        for (n = e; n.parentNode; ) n = n.parentNode;
        for (n = n.querySelectorAll("input[name=" + JSON.stringify("" + t) + '][type="radio"]'), t = 0; t < n.length; t++) {
          var r = n[t];
          if (r !== e && r.form === e.form) {
            var l = Il(r);
            if (!l) throw Error(j(90));
            ha(r), Oo(r, l);
          }
        }
      }
      break;
    case "textarea":
      ga(e, n);
      break;
    case "select":
      t = n.value, t != null && mn(e, !!n.multiple, t, !1);
  }
};
Sa = xs;
Ca = Xt;
var Bp = { usingClientEntryPoint: !1, Events: [kr, sn, Il, ja, ka, xs] }, Un = { findFiberByHostInstance: Wt, bundleType: 0, version: "18.3.1", rendererPackageName: "react-dom" }, Vp = { bundleType: Un.bundleType, version: Un.version, rendererPackageName: Un.rendererPackageName, rendererConfig: Un.rendererConfig, overrideHookState: null, overrideHookStateDeletePath: null, overrideHookStateRenamePath: null, overrideProps: null, overridePropsDeletePath: null, overridePropsRenamePath: null, setErrorHandler: null, setSuspenseHandler: null, scheduleUpdate: null, currentDispatcherRef: dt.ReactCurrentDispatcher, findHostInstanceByFiber: function(e) {
  return e = Na(e), e === null ? null : e.stateNode;
}, findFiberByHostInstance: Un.findFiberByHostInstance || $p, findHostInstancesForRefresh: null, scheduleRefresh: null, scheduleRoot: null, setRefreshHandler: null, getCurrentFiber: null, reconcilerVersion: "18.3.1-next-f1338f8080-20240426" };
if (typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ < "u") {
  var Hr = __REACT_DEVTOOLS_GLOBAL_HOOK__;
  if (!Hr.isDisabled && Hr.supportsFiber) try {
    zl = Hr.inject(Vp), be = Hr;
  } catch {
  }
}
Le.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED = Bp;
Le.createPortal = function(e, t) {
  var n = 2 < arguments.length && arguments[2] !== void 0 ? arguments[2] : null;
  if (!Es(t)) throw Error(j(200));
  return Up(e, t, null, n);
};
Le.createRoot = function(e, t) {
  if (!Es(e)) throw Error(j(299));
  var n = !1, r = "", l = sf;
  return t != null && (t.unstable_strictMode === !0 && (n = !0), t.identifierPrefix !== void 0 && (r = t.identifierPrefix), t.onRecoverableError !== void 0 && (l = t.onRecoverableError)), t = ks(e, 1, !1, null, null, n, !1, r, l), e[at] = t.current, ur(e.nodeType === 8 ? e.parentNode : e), new Cs(t);
};
Le.findDOMNode = function(e) {
  if (e == null) return null;
  if (e.nodeType === 1) return e;
  var t = e._reactInternals;
  if (t === void 0)
    throw typeof e.render == "function" ? Error(j(188)) : (e = Object.keys(e).join(","), Error(j(268, e)));
  return e = Na(t), e = e === null ? null : e.stateNode, e;
};
Le.flushSync = function(e) {
  return Xt(e);
};
Le.hydrate = function(e, t, n) {
  if (!Vl(t)) throw Error(j(200));
  return Ql(null, e, t, !0, n);
};
Le.hydrateRoot = function(e, t, n) {
  if (!Es(e)) throw Error(j(405));
  var r = n != null && n.hydratedSources || null, l = !1, o = "", i = sf;
  if (n != null && (n.unstable_strictMode === !0 && (l = !0), n.identifierPrefix !== void 0 && (o = n.identifierPrefix), n.onRecoverableError !== void 0 && (i = n.onRecoverableError)), t = of(t, null, e, 1, n ?? null, l, !1, o, i), e[at] = t.current, ur(e), r) for (e = 0; e < r.length; e++) n = r[e], l = n._getVersion, l = l(n._source), t.mutableSourceEagerHydrationData == null ? t.mutableSourceEagerHydrationData = [n, l] : t.mutableSourceEagerHydrationData.push(
    n,
    l
  );
  return new Bl(t);
};
Le.render = function(e, t, n) {
  if (!Vl(t)) throw Error(j(200));
  return Ql(null, e, t, !1, n);
};
Le.unmountComponentAtNode = function(e) {
  if (!Vl(e)) throw Error(j(40));
  return e._reactRootContainer ? (Xt(function() {
    Ql(null, null, e, !1, function() {
      e._reactRootContainer = null, e[at] = null;
    });
  }), !0) : !1;
};
Le.unstable_batchedUpdates = xs;
Le.unstable_renderSubtreeIntoContainer = function(e, t, n, r) {
  if (!Vl(n)) throw Error(j(200));
  if (e == null || e._reactInternals === void 0) throw Error(j(38));
  return Ql(e, t, n, !1, r);
};
Le.version = "18.3.1-next-f1338f8080-20240426";
function uf() {
  if (!(typeof __REACT_DEVTOOLS_GLOBAL_HOOK__ > "u" || typeof __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE != "function"))
    try {
      __REACT_DEVTOOLS_GLOBAL_HOOK__.checkDCE(uf);
    } catch (e) {
      console.error(e);
    }
}
uf(), sa.exports = Le;
var Qp = sa.exports, Du = Qp;
To.createRoot = Du.createRoot, To.hydrateRoot = Du.hydrateRoot;
var af = { exports: {} }, Kl = {};
/**
 * @license React
 * react-jsx-runtime.production.min.js
 *
 * Copyright (c) Facebook, Inc. and its affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
var Kp = R, Xp = Symbol.for("react.element"), Yp = Symbol.for("react.fragment"), Gp = Object.prototype.hasOwnProperty, Jp = Kp.__SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED.ReactCurrentOwner, Zp = { key: !0, ref: !0, __self: !0, __source: !0 };
function cf(e, t, n) {
  var r, l = {}, o = null, i = null;
  n !== void 0 && (o = "" + n), t.key !== void 0 && (o = "" + t.key), t.ref !== void 0 && (i = t.ref);
  for (r in t) Gp.call(t, r) && !Zp.hasOwnProperty(r) && (l[r] = t[r]);
  if (e && e.defaultProps) for (r in t = e.defaultProps, t) l[r] === void 0 && (l[r] = t[r]);
  return { $$typeof: Xp, type: e, key: o, ref: i, props: l, _owner: Jp.current };
}
Kl.Fragment = Yp;
Kl.jsx = cf;
Kl.jsxs = cf;
af.exports = Kl;
var v = af.exports;
const qp = {}, Wu = (e) => {
  let t;
  const n = /* @__PURE__ */ new Set(), r = (p, h) => {
    const m = typeof p == "function" ? p(t) : p;
    if (!Object.is(m, t)) {
      const x = t;
      t = h ?? (typeof m != "object" || m === null) ? m : Object.assign({}, t, m), n.forEach((_) => _(t, x));
    }
  }, l = () => t, u = { setState: r, getState: l, getInitialState: () => a, subscribe: (p) => (n.add(p), () => n.delete(p)), destroy: () => {
    (qp ? "production" : void 0) !== "production" && console.warn(
      "[DEPRECATED] The `destroy` method will be unsupported in a future version. Instead use unsubscribe function returned by subscribe. Everything will be garbage-collected if store is garbage-collected."
    ), n.clear();
  } }, a = t = e(r, l, u);
  return u;
}, bp = (e) => e ? Wu(e) : Wu;
var ff = { exports: {} }, df = {}, pf = { exports: {} }, mf = {};
/**
 * @license React
 * use-sync-external-store-shim.production.js
 *
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
var Fn = R;
function em(e, t) {
  return e === t && (e !== 0 || 1 / e === 1 / t) || e !== e && t !== t;
}
var tm = typeof Object.is == "function" ? Object.is : em, nm = Fn.useState, rm = Fn.useEffect, lm = Fn.useLayoutEffect, om = Fn.useDebugValue;
function im(e, t) {
  var n = t(), r = nm({ inst: { value: n, getSnapshot: t } }), l = r[0].inst, o = r[1];
  return lm(
    function() {
      l.value = n, l.getSnapshot = t, So(l) && o({ inst: l });
    },
    [e, n, t]
  ), rm(
    function() {
      return So(l) && o({ inst: l }), e(function() {
        So(l) && o({ inst: l });
      });
    },
    [e]
  ), om(n), n;
}
function So(e) {
  var t = e.getSnapshot;
  e = e.value;
  try {
    var n = t();
    return !tm(e, n);
  } catch {
    return !0;
  }
}
function sm(e, t) {
  return t();
}
var um = typeof window > "u" || typeof window.document > "u" || typeof window.document.createElement > "u" ? sm : im;
mf.useSyncExternalStore = Fn.useSyncExternalStore !== void 0 ? Fn.useSyncExternalStore : um;
pf.exports = mf;
var am = pf.exports;
/**
 * @license React
 * use-sync-external-store-shim/with-selector.production.js
 *
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */
var Xl = R, cm = am;
function fm(e, t) {
  return e === t && (e !== 0 || 1 / e === 1 / t) || e !== e && t !== t;
}
var dm = typeof Object.is == "function" ? Object.is : fm, pm = cm.useSyncExternalStore, mm = Xl.useRef, hm = Xl.useEffect, vm = Xl.useMemo, gm = Xl.useDebugValue;
df.useSyncExternalStoreWithSelector = function(e, t, n, r, l) {
  var o = mm(null);
  if (o.current === null) {
    var i = { hasValue: !1, value: null };
    o.current = i;
  } else i = o.current;
  o = vm(
    function() {
      function u(x) {
        if (!a) {
          if (a = !0, p = x, x = r(x), l !== void 0 && i.hasValue) {
            var _ = i.value;
            if (l(_, x))
              return h = _;
          }
          return h = x;
        }
        if (_ = h, dm(p, x)) return _;
        var k = r(x);
        return l !== void 0 && l(_, k) ? (p = x, _) : (p = x, h = k);
      }
      var a = !1, p, h, m = n === void 0 ? null : n;
      return [
        function() {
          return u(t());
        },
        m === null ? void 0 : function() {
          return u(m());
        }
      ];
    },
    [t, n, r, l]
  );
  var s = pm(e, o[0], o[1]);
  return hm(
    function() {
      i.hasValue = !0, i.value = s;
    },
    [s]
  ), gm(s), s;
};
ff.exports = df;
var ym = ff.exports;
const xm = /* @__PURE__ */ Zu(ym), hf = {}, { useDebugValue: wm } = No, { useSyncExternalStoreWithSelector: _m } = xm;
let Au = !1;
const jm = (e) => e;
function vf(e, t = jm, n) {
  (hf ? "production" : void 0) !== "production" && n && !Au && (console.warn(
    "[DEPRECATED] Use `createWithEqualityFn` instead of `create` or use `useStoreWithEqualityFn` instead of `useStore`. They can be imported from 'zustand/traditional'. https://github.com/pmndrs/zustand/discussions/1937"
  ), Au = !0);
  const r = _m(
    e.subscribe,
    e.getState,
    e.getServerState || e.getInitialState,
    t,
    n
  );
  return wm(r), r;
}
const Uu = (e) => {
  (hf ? "production" : void 0) !== "production" && typeof e != "function" && console.warn(
    "[DEPRECATED] Passing a vanilla store will be unsupported in a future version. Instead use `import { useStore } from 'zustand'`."
  );
  const t = typeof e == "function" ? bp(e) : e, n = (r, l) => vf(t, r, l);
  return Object.assign(n, t), n;
}, km = (e) => e ? Uu(e) : Uu, gf = R.createContext(null);
function T(e) {
  const t = R.useContext(gf);
  if (!t)
    throw new Error("useCaptionStoreContext must be used within StoreContext.Provider");
  return vf(t, e);
}
function Sm(e, t = 3, n = 0.3) {
  if (e.length === 0) return [];
  const r = /* @__PURE__ */ new Map();
  for (const i of e) {
    const s = i.track || 0;
    r.has(s) || r.set(s, []), r.get(s).push(i);
  }
  const l = [];
  let o = 0;
  for (const i of [...r.keys()].sort((s, u) => s - u)) {
    const s = r.get(i);
    let u = 0, a = [];
    for (let p = 0; p < s.length; p++) {
      const h = s[p];
      if (p === 0)
        u = 1, a = [h];
      else {
        const m = h.start - s[p - 1].end;
        u >= t || m > n ? (a.length > 0 && (l.push({
          words: a,
          start: a[0].start,
          end: a[a.length - 1].end,
          segmentIdx: o,
          track: i
        }), o++), u = 1, a = [h]) : (u++, a.push(h));
      }
    }
    a.length > 0 && (l.push({
      words: a,
      start: a[0].start,
      end: a[a.length - 1].end,
      segmentIdx: o,
      track: i
    }), o++);
  }
  return l;
}
function Cm(e) {
  const t = e.replace("#", "");
  return [
    parseInt(t.substring(0, 2), 16),
    parseInt(t.substring(2, 4), 16),
    parseInt(t.substring(4, 6), 16)
  ];
}
function Em() {
  const e = R.useRef(null), t = T(
    (f) => f.previewFrameBase64
  ), n = T((f) => f.frameAtlas), r = T((f) => f.frameWidth), l = T((f) => f.frameHeight), o = T((f) => f.editedTimings), i = T((f) => f.style), s = T((f) => f.previewTime), u = T((f) => f.fps), a = R.useRef(/* @__PURE__ */ new Map()), p = R.useRef(null);
  R.useEffect(() => {
    if (!t) return;
    const f = new Image();
    f.src = `data:image/jpeg;base64,${t}`, f.onload = () => {
      p.current = f;
    };
  }, [t]);
  const h = R.useCallback(
    (f) => {
      if (!n || n.frames.length === 0) return null;
      const c = Math.floor(f * u), d = Math.min(
        Math.floor(c / n.interval),
        n.frames.length - 1
      ), y = Math.max(0, d), S = a.current.get(y);
      if (S && S.complete) return S;
      if (!S) {
        const w = new Image();
        w.src = `data:image/jpeg;base64,${n.frames[y]}`, a.current.set(y, w), w.onload = () => {
          e.current && x(s);
        };
      }
      return null;
    },
    [n, u]
  ), m = (f) => 1 + 2.70158 * Math.pow(f - 1, 3) + 1.70158 * Math.pow(f - 1, 2), x = R.useCallback(
    (f) => {
      const c = e.current;
      if (!c) return;
      const d = c.getContext("2d");
      if (!d) return;
      const y = c.width, S = c.height;
      d.clearRect(0, 0, y, S);
      const w = h(f);
      if (w && w.complete ? d.drawImage(w, 0, 0, y, S) : p.current ? d.drawImage(p.current, 0, 0, y, S) : (d.fillStyle = "#000", d.fillRect(0, 0, y, S)), o.length === 0) return;
      const g = Sm(o), C = y / (r || 1920);
      for (const z of g) {
        if (f < z.start || f > z.end) continue;
        const M = z.words.filter((W) => !W.override_hidden);
        if (M.length === 0) continue;
        const U = Math.round(i.font_size * C), ce = i.uppercase, $ = i.anim_duration, V = $ > 0 ? Math.min(1, (f - z.start) / $) : 1, Fe = m(V);
        let fe = 0;
        for (let W = 0; W < M.length; W++)
          M[W].start <= f && (fe = W);
        const de = "Impact, Arial Black, sans-serif", E = [];
        for (const W of M) {
          const q = W.override_text || W.word, nt = ce ? q.toUpperCase() : q;
          let _e = U;
          W.override_font_size ? _e = Math.round(W.override_font_size * C) : W.override_scale && (_e = Math.round(U * W.override_scale)), d.font = `bold ${_e}px ${de}`;
          const pt = d.measureText(nt);
          E.push({ text: nt, width: pt.width, fontSize: _e, word: W });
        }
        d.font = `bold ${U}px ${de}`;
        const P = d.measureText(" ").width, L = E.reduce((W, q) => W + q.width, 0) + P * Math.max(0, E.length - 1), H = y * 0.9, Z = L > H ? H / L : 1, tt = Fe * Z, Rt = i.y_position + z.track * 0.12, F = S * Math.min(0.95, Rt);
        d.save(), d.translate(y / 2, F), d.scale(tt, tt);
        let le = -L / 2;
        for (let W = 0; W < E.length; W++) {
          const q = E[W], nt = W === fe;
          d.font = `bold ${q.fontSize}px ${de}`;
          let _e;
          if (nt)
            if (q.word.override_highlight)
              _e = q.word.override_highlight;
            else {
              const qt = (Fm(q.word.word.toLowerCase()) & 2147483647) % i.highlight_colors.length;
              _e = i.highlight_colors[qt];
            }
          else
            _e = q.word.override_color || i.fill_color;
          const pt = q.word.override_stroke_color || i.stroke_color, Mn = Math.round(
            (q.word.override_stroke_width ?? i.stroke_width) * C
          );
          if (i.shadow_opacity > 0) {
            const [qt, Sf, Cf] = Cm(i.shadow_color);
            d.save(), d.shadowColor = `rgba(${qt},${Sf},${Cf},${i.shadow_opacity})`, d.shadowBlur = i.shadow_blur * C, d.shadowOffsetX = i.shadow_offset_x * C, d.shadowOffsetY = i.shadow_offset_y * C, d.fillStyle = _e, d.fillText(q.text, le, 0), d.restore();
          }
          Mn > 0 && (d.strokeStyle = pt, d.lineWidth = Mn, d.lineJoin = "round", d.strokeText(q.text, le, 0)), d.fillStyle = _e, d.fillText(q.text, le, 0), le += q.width + P;
        }
        d.restore();
      }
    },
    [o, i, r, l, h]
  );
  R.useEffect(() => {
    x(s);
  }, [s, x]);
  const _ = Math.min(480, r || 480), k = (l || 1080) / (r || 1920), I = Math.round(_ * k);
  return /* @__PURE__ */ v.jsx(
    "canvas",
    {
      ref: e,
      width: _,
      height: I,
      className: "jcs:border jcs:border-border jcs:rounded jcs:max-w-full jcs:h-auto"
    }
  );
}
function Fm(e) {
  let t = 0;
  for (let n = 0; n < e.length; n++) {
    const r = e.charCodeAt(n);
    t = (t << 5) - t + r, t |= 0;
  }
  return t;
}
function Nm(e) {
  if (e < 60) return e.toFixed(1) + "s";
  const t = Math.floor(e / 60), n = (e % 60).toFixed(1);
  return `${t}:${n.padStart(4, "0")}`;
}
function $u(e) {
  const t = Math.max(0, e);
  if (t < 60) return t.toFixed(1) + "s";
  const n = Math.floor(t / 60), r = (t % 60).toFixed(1);
  return `${n}:${r.padStart(4, "0")}`;
}
function Tm(e) {
  return e <= 2 ? 0.1 : e <= 5 ? 0.25 : e <= 15 ? 0.5 : e <= 60 ? 1 : e <= 300 ? 5 : 10;
}
function Pm() {
  const e = T((w) => w.previewTime), t = T((w) => w.setPreviewTime), n = T((w) => w.isPlaying), r = T((w) => w.togglePlay), l = T((w) => w.setIsPlaying), o = T((w) => w.stepFrame), i = T((w) => w.duration), s = T((w) => w.editedTimings), u = T((w) => w.audioDataUrl), a = R.useRef(0), p = R.useRef(0), h = R.useRef(e), m = R.useRef(null), x = !!u;
  R.useEffect(() => {
    h.current = e;
  }, [e]), R.useEffect(() => {
    m.current && (u ? (m.current.src = u, m.current.load()) : m.current.removeAttribute("src"));
  }, [u]);
  const _ = R.useCallback((w) => {
    m.current && x && (m.current.currentTime = w);
  }, [x]);
  R.useEffect(() => {
    if (!n) {
      a.current && cancelAnimationFrame(a.current), m.current && x && m.current.pause();
      return;
    }
    if (x) {
      const w = m.current;
      w.currentTime = h.current, w.play().catch(() => {
      });
      const g = () => {
        const C = w.currentTime;
        if (C >= k) {
          t(0), l(!1), w.pause();
          return;
        }
        t(C), a.current = requestAnimationFrame(g);
      };
      return a.current = requestAnimationFrame(g), () => {
        a.current && cancelAnimationFrame(a.current), w.pause();
      };
    } else {
      p.current = performance.now();
      const w = (g) => {
        const C = (g - p.current) / 1e3;
        p.current = g;
        const z = h.current + C;
        if (z > k) {
          t(0), l(!1);
          return;
        }
        t(z), a.current = requestAnimationFrame(w);
      };
      return a.current = requestAnimationFrame(w), () => {
        a.current && cancelAnimationFrame(a.current);
      };
    }
  }, [n, x]);
  const k = R.useMemo(() => i > 0 ? i : s.length > 0 ? Math.max(...s.map((w) => w.end)) : 0, [i, s]), I = k > 0 ? e / k * 100 : 0, f = (w) => {
    const g = parseFloat(w.target.value);
    t(g), _(g);
  }, c = () => {
    t(0), _(0);
  }, d = () => {
    t(k), _(k);
  }, y = () => {
    o(-1), requestAnimationFrame(() => _(h.current));
  }, S = () => {
    o(1), requestAnimationFrame(() => _(h.current));
  };
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1.5 jcs:w-full jcs:px-2", children: [
    /* @__PURE__ */ v.jsx("audio", { ref: m, preload: "auto", style: { display: "none" } }),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs-transport-btn",
        onClick: c,
        title: "Jump to start",
        children: "⏮"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs-transport-btn",
        onClick: y,
        title: "Step back 1 frame",
        children: "⏴"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs-transport-btn jcs-play-btn",
        onClick: r,
        title: n ? "Pause" : "Play",
        children: n ? "⏸" : "▶"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs-transport-btn",
        onClick: S,
        title: "Step forward 1 frame",
        children: "⏵"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs-transport-btn",
        onClick: d,
        title: "Jump to end",
        children: "⏭"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "input",
      {
        type: "range",
        className: "jcs-scrubber jcs:flex-1",
        min: 0,
        max: k || 1,
        step: 1e-3,
        value: e,
        onChange: f,
        style: { "--jcs-scrub-pct": `${I}%` }
      }
    ),
    /* @__PURE__ */ v.jsxs("span", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:tabular-nums jcs:whitespace-nowrap", children: [
      $u(e),
      " / ",
      $u(k)
    ] })
  ] });
}
function zm() {
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:flex-col jcs:gap-1 jcs:p-2 jcs:items-center jcs:border-b jcs:border-border", children: [
    /* @__PURE__ */ v.jsx("div", { className: "jcs:flex jcs:justify-center jcs:w-full", children: /* @__PURE__ */ v.jsx(Em, {}) }),
    /* @__PURE__ */ v.jsx("p", { className: "jcs:text-[9px] jcs:text-text-dim jcs:text-center jcs:m-0", children: "Canvas preview is approximate. Actual rendering uses Pillow with exact fonts." }),
    /* @__PURE__ */ v.jsx(Pm, {})
  ] });
}
const ji = {
  font_path: "C:/Windows/Fonts/impact.ttf",
  font_size: 120,
  fill_color: "#FFFFFF",
  highlight_colors: ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
  stroke_color: "#000000",
  stroke_width: 8,
  shadow_color: "#000000",
  shadow_opacity: 0.6,
  shadow_offset_x: 4,
  shadow_offset_y: 4,
  shadow_blur: 6,
  anim_duration: 0.15,
  y_position: 0.45,
  uppercase: !0
}, ki = {
  Hormozi: {
    font_path: "C:/Windows/Fonts/impact.ttf",
    font_size: 120,
    fill_color: "#FFFFFF",
    highlight_colors: ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
    stroke_color: "#000000",
    stroke_width: 8,
    shadow_color: "#000000",
    shadow_opacity: 0.6,
    shadow_offset_x: 4,
    shadow_offset_y: 4,
    shadow_blur: 6,
    anim_duration: 0.15,
    y_position: 0.45,
    uppercase: !0
  },
  MrBeast: {
    font_path: "C:/Windows/Fonts/impact.ttf",
    font_size: 160,
    fill_color: "#FFFFFF",
    highlight_colors: ["#FFD700", "#FF4444", "#00FF00", "#FFFFFF"],
    stroke_color: "#000000",
    stroke_width: 12,
    shadow_color: "#000000",
    shadow_opacity: 0.6,
    shadow_offset_x: 4,
    shadow_offset_y: 4,
    shadow_blur: 8,
    anim_duration: 0.12,
    y_position: 0.42,
    uppercase: !0
  },
  Clean: {
    font_path: "C:/Windows/Fonts/segoeui.ttf",
    font_size: 72,
    fill_color: "#FFFFFF",
    highlight_colors: ["#87CEEB", "#90EE90", "#FFFF99", "#FFB6C1"],
    stroke_color: "#000000",
    stroke_width: 0,
    shadow_color: "#000000",
    shadow_opacity: 0.5,
    shadow_offset_x: 0,
    shadow_offset_y: 0,
    shadow_blur: 12,
    anim_duration: 0,
    y_position: 0.75,
    uppercase: !1
  },
  Netflix: {
    font_path: "C:/Windows/Fonts/arial.ttf",
    font_size: 64,
    fill_color: "#FFFFFF",
    highlight_colors: ["#FFFFFF", "#FFFFFF", "#FFFFFF", "#FFFFFF"],
    stroke_color: "#000000",
    stroke_width: 0,
    shadow_color: "#000000",
    shadow_opacity: 0.5,
    shadow_offset_x: 0,
    shadow_offset_y: 0,
    shadow_blur: 4,
    anim_duration: 0,
    y_position: 0.88,
    uppercase: !1
  },
  Neon: {
    font_path: "C:/Windows/Fonts/impact.ttf",
    font_size: 120,
    fill_color: "#FFFFFF",
    highlight_colors: ["#FF00FF", "#00FFFF", "#39FF14", "#FF6600"],
    stroke_color: "#2D0040",
    stroke_width: 6,
    shadow_color: "#8800FF",
    shadow_opacity: 0.7,
    shadow_offset_x: 0,
    shadow_offset_y: 0,
    shadow_blur: 16,
    anim_duration: 0.15,
    y_position: 0.45,
    uppercase: !0
  },
  "Pop Color": {
    font_path: "C:/Windows/Fonts/arialbd.ttf",
    font_size: 110,
    fill_color: "#FFFFFF",
    highlight_colors: ["#FF69B4", "#4488FF", "#FFD700", "#00CC66"],
    stroke_color: "#000000",
    stroke_width: 10,
    shadow_color: "#000000",
    shadow_opacity: 0.6,
    shadow_offset_x: 4,
    shadow_offset_y: 4,
    shadow_blur: 6,
    anim_duration: 0.18,
    y_position: 0.45,
    uppercase: !0
  },
  Podcast: {
    font_path: "C:/Windows/Fonts/segoeuib.ttf",
    font_size: 80,
    fill_color: "#FFFFFF",
    highlight_colors: ["#6699CC", "#66AA66", "#CC8844", "#CC6644"],
    stroke_color: "#333333",
    stroke_width: 4,
    shadow_color: "#000000",
    shadow_opacity: 0.5,
    shadow_offset_x: 2,
    shadow_offset_y: 2,
    shadow_blur: 8,
    anim_duration: 0,
    y_position: 0.8,
    uppercase: !1
  },
  Luxury: {
    font_path: "C:/Windows/Fonts/georgia.ttf",
    font_size: 90,
    fill_color: "#FFFFFF",
    highlight_colors: ["#FFD700", "#FFFDD0", "#F5DEB3", "#FFFFFF"],
    stroke_color: "#1A1A1A",
    stroke_width: 2,
    shadow_color: "#000000",
    shadow_opacity: 0.5,
    shadow_offset_x: 2,
    shadow_offset_y: 2,
    shadow_blur: 10,
    anim_duration: 0.1,
    y_position: 0.5,
    uppercase: !0
  }
}, Mm = ["Custom", ...Object.keys(ki)], El = [
  "#4a9eff",
  "#4adf7f",
  "#f0a040",
  "#e05577",
  "#aa66dd",
  "#44ccbb"
], Hu = [
  "#4a9eff",
  "#4adf7f",
  "#f0a040",
  "#e05577",
  "#aa66dd",
  "#44ccbb"
];
function Lm() {
  const e = T((a) => a.style), t = T((a) => a.fontList), n = T((a) => a.updateStyle), r = T((a) => a.presetName), l = T((a) => a.setPreset), o = T((a) => a.styleExpanded), i = T((a) => a.toggleStyleExpanded), s = (a) => {
    const p = a.replace(/\\/g, "/").split("/");
    return p[p.length - 1];
  }, u = (a) => {
    l(a), a !== "Custom" && ki[a] && (n({ ...ki[a] }), l(a));
  };
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:px-2 jcs:py-1.5 jcs:border-b jcs:border-border jcs:bg-panel jcs:flex jcs:flex-col jcs:gap-1", children: [
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap", children: [
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "Preset:" }),
        /* @__PURE__ */ v.jsx(
          "select",
          {
            className: "jcs:text-[10px] jcs:w-[90px]",
            value: r,
            onChange: (a) => u(a.target.value),
            children: Mm.map((a) => /* @__PURE__ */ v.jsx("option", { value: a, children: a }, a))
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "Font:" }),
        /* @__PURE__ */ v.jsx(
          "select",
          {
            className: "jcs:text-[10px] jcs:w-[100px]",
            value: e.font_path,
            onChange: (a) => n({ font_path: a.target.value }),
            children: t.map((a) => /* @__PURE__ */ v.jsx("option", { value: a, children: s(a) }, a))
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "Size:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "number",
            className: "jcs:text-[10px] jcs:w-[48px]",
            value: e.font_size,
            min: 20,
            max: 400,
            step: 4,
            onChange: (a) => n({ font_size: Math.round(parseFloat(a.target.value) || 20) })
          }
        )
      ] })
    ] }),
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap", children: [
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "Fill:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: e.fill_color,
            onChange: (a) => n({ fill_color: a.target.value }),
            className: "jcs:w-6 jcs:h-5"
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "Stroke:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: e.stroke_color,
            onChange: (a) => n({ stroke_color: a.target.value }),
            className: "jcs:w-6 jcs:h-5"
          }
        ),
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-dim jcs:text-[9px]", children: "/" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "number",
            className: "jcs:text-[10px] jcs:w-[36px]",
            value: e.stroke_width,
            min: 0,
            max: 30,
            step: 1,
            onChange: (a) => n({ stroke_width: Math.round(parseFloat(a.target.value) || 0) })
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[9px]", children: "HL:" }),
        e.highlight_colors.map((a, p) => /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: a,
            onChange: (h) => {
              const m = [...e.highlight_colors];
              m[p] = h.target.value, n({ highlight_colors: m });
            },
            className: "jcs:w-5 jcs:h-5",
            title: `Highlight ${p + 1}`
          },
          p
        ))
      ] }),
      /* @__PURE__ */ v.jsx("div", { className: "jcs:flex-1" }),
      /* @__PURE__ */ v.jsx("button", { className: "jcs:text-[10px]", onClick: i, children: o ? "▲ Less" : "▼ More" })
    ] })
  ] });
}
function en({
  label: e,
  value: t,
  min: n,
  max: r,
  step: l,
  onChange: o,
  decimals: i = 1
}) {
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2", children: [
    /* @__PURE__ */ v.jsx("label", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0", children: e }),
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1.5 jcs:flex-1", children: [
      /* @__PURE__ */ v.jsx(
        "input",
        {
          type: "range",
          min: n,
          max: r,
          step: l,
          value: t,
          onChange: (s) => o(parseFloat(s.target.value)),
          className: "jcs:flex-1"
        }
      ),
      /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[32px] jcs:text-right jcs:tabular-nums", children: t.toFixed(i) })
    ] })
  ] });
}
function Rm({
  label: e,
  checked: t,
  onChange: n
}) {
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2", children: [
    /* @__PURE__ */ v.jsx("label", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0", children: e }),
    /* @__PURE__ */ v.jsx(
      "input",
      {
        type: "checkbox",
        checked: t,
        onChange: (r) => n(r.target.checked)
      }
    )
  ] });
}
function Im({ label: e, value: t, onChange: n }) {
  const [r, l] = R.useState(t), o = (s) => {
    const u = s.target.value;
    l(u), n(u);
  }, i = (s) => {
    const u = s.target.value;
    l(u), /^#[0-9a-fA-F]{6}$/.test(u) && n(u);
  };
  return t !== r && /^#[0-9a-fA-F]{6}$/.test(t) && l(t), /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2", children: [
    /* @__PURE__ */ v.jsx("label", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[80px] jcs:text-right jcs:shrink-0", children: e }),
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
      /* @__PURE__ */ v.jsx(
        "input",
        {
          type: "color",
          value: t,
          onChange: o,
          className: "jcs:w-7 jcs:h-5"
        }
      ),
      /* @__PURE__ */ v.jsx(
        "input",
        {
          type: "text",
          value: r,
          onChange: i,
          className: "jcs:text-[9px] jcs:w-[70px] jcs:font-mono"
        }
      )
    ] })
  ] });
}
function Om() {
  const e = T((n) => n.style), t = T((n) => n.updateStyle);
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:px-2 jcs:py-1.5 jcs:border-b jcs:border-border jcs:bg-panel jcs:flex jcs:flex-col jcs:gap-1", children: [
    /* @__PURE__ */ v.jsx("div", { className: "jcs:text-[9px] jcs:text-text-dim jcs:uppercase jcs:tracking-wider jcs:mb-0.5", children: "Shadow" }),
    /* @__PURE__ */ v.jsx(
      Im,
      {
        label: "Shadow Color",
        value: e.shadow_color,
        onChange: (n) => t({ shadow_color: n })
      }
    ),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Opacity",
        value: e.shadow_opacity,
        min: 0,
        max: 1,
        step: 0.05,
        decimals: 2,
        onChange: (n) => t({ shadow_opacity: n })
      }
    ),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Blur",
        value: e.shadow_blur,
        min: 0,
        max: 30,
        step: 0.5,
        onChange: (n) => t({ shadow_blur: n })
      }
    ),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Offset X",
        value: e.shadow_offset_x,
        min: -20,
        max: 20,
        step: 1,
        decimals: 0,
        onChange: (n) => t({ shadow_offset_x: Math.round(n) })
      }
    ),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Offset Y",
        value: e.shadow_offset_y,
        min: -20,
        max: 20,
        step: 1,
        decimals: 0,
        onChange: (n) => t({ shadow_offset_y: Math.round(n) })
      }
    ),
    /* @__PURE__ */ v.jsx("div", { className: "jcs:text-[9px] jcs:text-text-dim jcs:uppercase jcs:tracking-wider jcs:mt-1 jcs:mb-0.5", children: "Animation & Layout" }),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Anim Duration",
        value: e.anim_duration,
        min: 0,
        max: 1,
        step: 0.01,
        decimals: 2,
        onChange: (n) => t({ anim_duration: n })
      }
    ),
    /* @__PURE__ */ v.jsx(
      en,
      {
        label: "Y Position",
        value: e.y_position,
        min: 0,
        max: 1,
        step: 0.01,
        decimals: 2,
        onChange: (n) => t({ y_position: n })
      }
    ),
    /* @__PURE__ */ v.jsx(
      Rm,
      {
        label: "Uppercase",
        checked: e.uppercase,
        onChange: (n) => t({ uppercase: n })
      }
    )
  ] });
}
function Dm() {
  const e = T((a) => a.zoomLevel), t = T((a) => a.setZoomLevel), n = T((a) => a.setScrollLeft), r = T((a) => a.editedTimings), l = r.length, o = new Set(r.map((a) => a.segment_idx ?? -1)).size, i = r.length > 0 ? Math.min(...r.map((a) => a.start)) : 0, u = (r.length > 0 ? Math.max(...r.map((a) => a.end)) : 0) - i;
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1.5 jcs:px-2 jcs:py-1 jcs:border-b jcs:border-border jcs:bg-panel", children: [
    /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-muted jcs:text-[10px] jcs:mr-0.5", children: "Zoom:" }),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs:w-6 jcs:h-5 jcs:p-0 jcs:text-sm jcs:leading-[18px] jcs:text-center",
        onClick: () => t(e / 1.2),
        children: "−"
      }
    ),
    /* @__PURE__ */ v.jsxs("span", { className: "jcs:text-text-muted jcs:text-[10px] jcs:min-w-[36px] jcs:text-center jcs:tabular-nums", children: [
      Math.round(e * 100),
      "%"
    ] }),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs:w-6 jcs:h-5 jcs:p-0 jcs:text-sm jcs:leading-[18px] jcs:text-center",
        onClick: () => t(e * 1.2),
        children: "+"
      }
    ),
    /* @__PURE__ */ v.jsx(
      "button",
      {
        className: "jcs:text-[9px] jcs:px-1.5 jcs:py-0.5",
        onClick: () => {
          t(1), n(0);
        },
        children: "Fit"
      }
    ),
    /* @__PURE__ */ v.jsx("div", { className: "jcs:flex-1" }),
    /* @__PURE__ */ v.jsxs("span", { className: "jcs:text-text-dim jcs:text-[10px] jcs:tabular-nums", children: [
      l,
      " words · ",
      o,
      " segs · ",
      u.toFixed(1),
      "s"
    ] })
  ] });
}
const Wm = 28;
function Am({ minTime: e, maxTime: t, pps: n }) {
  const r = T((s) => s.seekToTime), l = Tm(t - e), o = [];
  for (let s = Math.ceil(e / l) * l; s <= t; s += l)
    o.push({
      x: (s - e) * n,
      label: Nm(s)
    });
  const i = R.useCallback(
    (s) => {
      const u = s.currentTarget.getBoundingClientRect(), p = (s.clientX - u.left) / n + e;
      r(Math.max(e, Math.min(t, p)));
    },
    [n, e, t, r]
  );
  return /* @__PURE__ */ v.jsx(
    "div",
    {
      className: "jcs:relative jcs:border-b jcs:border-border jcs:mb-1 jcs:cursor-pointer",
      style: { height: Wm },
      onClick: i,
      children: o.map((s, u) => /* @__PURE__ */ v.jsxs("div", { children: [
        /* @__PURE__ */ v.jsx(
          "div",
          {
            className: "jcs:absolute jcs:w-px jcs:bg-border-light",
            style: { left: s.x, top: 14, height: 12 }
          }
        ),
        /* @__PURE__ */ v.jsx(
          "div",
          {
            className: "jcs:absolute jcs:top-0 jcs:-translate-x-1/2 jcs:text-[9px] jcs:text-text-muted jcs:whitespace-nowrap",
            style: { left: s.x },
            children: s.label
          }
        )
      ] }, u))
    }
  );
}
const Um = 32, $m = 6;
function Hm({
  word: e,
  index: t,
  minTime: n,
  pps: r,
  segmentIdx: l
}) {
  const o = T((F) => F.selectedWordIndex), i = T((F) => F.moveWord), s = T((F) => F.resizeWord), u = T((F) => F.selectWord), a = T((F) => F.moveWordToTrack), p = T((F) => F.trackCount), h = T((F) => F.seekToTime), m = T((F) => F.editingWordIndex), x = T((F) => F.editingWordText), _ = T((F) => F.startEditingWord), k = T((F) => F.commitEditingWord), I = T((F) => F.cancelEditingWord), f = T((F) => F.setEditingWordText), c = T((F) => F.deleteWord), d = T((F) => F.duplicateWord), [y, S] = R.useState(null), w = R.useRef(null), g = m === t, C = o === t, z = e.override_hidden || !1, M = El[l % El.length], U = e.override_text || e.word, ce = e.override_color || e.override_highlight || e.override_scale || e.override_font_size || e.override_stroke_width || e.override_stroke_color || e.override_text || e.override_hidden, $ = (e.start - n) * r, V = Math.max((e.end - e.start) * r, 12), Fe = R.useRef(0), fe = R.useRef(0), de = R.useRef(0), E = R.useRef(!1), P = R.useRef(0), L = R.useRef(null);
  R.useEffect(() => {
    g && w.current && (w.current.focus(), w.current.select());
  }, [g]);
  const H = R.useCallback(
    (F) => {
      if (F.button === 2 || F.target.dataset.resize || g) return;
      F.preventDefault(), F.stopPropagation(), E.current = !1, Fe.current = $, fe.current = e.start;
      const le = F.clientX, W = e.end - e.start, q = (_e) => {
        const pt = _e.clientX - le;
        Math.abs(pt) > 3 && (E.current = !0);
        const qt = Math.max(0, Fe.current + pt) / r + n;
        i(t, qt, qt + W);
      }, nt = () => {
        document.removeEventListener("mousemove", q), document.removeEventListener("mouseup", nt), E.current || (P.current++, P.current === 1 ? L.current = setTimeout(() => {
          u(t), h(e.start), P.current = 0;
        }, 250) : P.current >= 2 && (L.current && clearTimeout(L.current), P.current = 0, u(t), _(t)));
      };
      document.addEventListener("mousemove", q), document.addEventListener("mouseup", nt);
    },
    [$, e.start, e.end, r, n, t, i, u, h, _, g]
  ), Z = R.useCallback(
    (F) => {
      F.preventDefault(), F.stopPropagation(), de.current = V, fe.current = e.start;
      const le = F.clientX, W = (nt) => {
        const _e = nt.clientX - le, pt = Math.max(12, de.current + _e), Mn = fe.current + pt / r;
        s(t, Mn);
      }, q = () => {
        document.removeEventListener("mousemove", W), document.removeEventListener("mouseup", q);
      };
      document.addEventListener("mousemove", W), document.addEventListener("mouseup", q);
    },
    [V, e.start, r, t, s]
  ), tt = R.useCallback((F) => {
    F.preventDefault(), F.stopPropagation(), S({ x: F.clientX, y: F.clientY });
  }, []), Ge = R.useCallback(
    (F) => {
      a(t, F), S(null);
    },
    [t, a]
  ), Rt = R.useCallback(
    (F) => {
      F.key === "Enter" ? k() : F.key === "Escape" && I();
    },
    [k, I]
  );
  return /* @__PURE__ */ v.jsxs(v.Fragment, { children: [
    /* @__PURE__ */ v.jsxs(
      "div",
      {
        className: `jcs:absolute jcs:rounded jcs:flex jcs:items-center jcs:justify-center jcs:overflow-hidden jcs:select-none jcs:transition-shadow jcs:cursor-grab jcs:hover:shadow-[0_0_0_2px_rgba(255,255,255,0.4)] ${C ? "jcs:outline jcs:outline-2 jcs:outline-white" : ""} ${z ? "jcs:opacity-40" : ""}`,
        style: {
          left: $,
          width: V,
          height: Um - 8,
          top: 4,
          background: z ? "#333" : M
        },
        onMouseDown: H,
        onContextMenu: tt,
        title: `"${U}" ${e.start.toFixed(3)}s → ${e.end.toFixed(3)}s`,
        children: [
          g ? /* @__PURE__ */ v.jsx(
            "input",
            {
              ref: w,
              type: "text",
              value: x,
              onChange: (F) => f(F.target.value),
              onKeyDown: Rt,
              onBlur: k,
              className: "jcs:bg-black/50 jcs:text-white jcs:text-[10px] jcs:font-semibold jcs:w-full jcs:h-full jcs:border-none jcs:outline-none jcs:text-center jcs:px-1",
              onClick: (F) => F.stopPropagation(),
              onMouseDown: (F) => F.stopPropagation()
            }
          ) : /* @__PURE__ */ v.jsx("span", { className: "jcs:text-white jcs:text-[10px] jcs:font-semibold jcs:pointer-events-none jcs:whitespace-nowrap jcs:text-ellipsis jcs:overflow-hidden jcs:px-1 jcs:pr-2", children: U }),
          ce && !g && /* @__PURE__ */ v.jsx("div", { className: "jcs:absolute jcs:top-0.5 jcs:right-2 jcs:w-1.5 jcs:h-1.5 jcs:rounded-full jcs:bg-warning" }),
          !g && /* @__PURE__ */ v.jsx(
            "div",
            {
              className: "jcs:absolute jcs:right-0 jcs:top-0 jcs:h-full jcs:cursor-ew-resize jcs:bg-white/15 jcs:rounded-r",
              "data-resize": "true",
              style: { width: $m },
              onMouseDown: Z
            }
          )
        ]
      }
    ),
    y && /* @__PURE__ */ v.jsxs(v.Fragment, { children: [
      /* @__PURE__ */ v.jsx(
        "div",
        {
          className: "jcs:fixed jcs:inset-0 jcs:z-[9999]",
          onClick: () => S(null)
        }
      ),
      /* @__PURE__ */ v.jsxs(
        "div",
        {
          className: "jcs:fixed jcs:z-[10000] jcs:bg-surface-raised jcs:border jcs:border-border-light jcs:rounded jcs:py-1 jcs:min-w-[120px] jcs:shadow-lg",
          style: { left: y.x, top: y.y },
          children: [
            /* @__PURE__ */ v.jsxs("div", { className: "jcs:px-3 jcs:py-1 jcs:text-text-muted jcs:text-[9px] jcs:border-b jcs:border-border", children: [
              '"',
              U,
              '"'
            ] }),
            /* @__PURE__ */ v.jsx(
              "div",
              {
                className: "jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:text-text jcs:hover:bg-[#3a3a5e]",
                onClick: () => {
                  d(t), S(null);
                },
                children: "Duplicate"
              }
            ),
            /* @__PURE__ */ v.jsx(
              "div",
              {
                className: "jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:text-red-400 jcs:hover:bg-[#5e3a3a]",
                onClick: () => {
                  c(t), S(null);
                },
                children: "Delete"
              }
            ),
            /* @__PURE__ */ v.jsx("div", { className: "jcs:border-t jcs:border-border jcs:my-1" }),
            /* @__PURE__ */ v.jsx("div", { className: "jcs:px-3 jcs:py-0.5 jcs:text-text-muted jcs:text-[9px]", children: "Move to:" }),
            Array.from({ length: p }, (F, le) => {
              const W = le === (e.track || 0);
              return /* @__PURE__ */ v.jsxs(
                "div",
                {
                  className: `jcs:px-3 jcs:py-1 jcs:text-[11px] jcs:cursor-pointer jcs:transition-colors ${W ? "jcs:text-text-dim jcs:italic jcs:cursor-default" : "jcs:text-text jcs:hover:bg-[#3a3a5e]"}`,
                  onClick: () => !W && Ge(le),
                  children: [
                    "Track ",
                    le + 1,
                    W ? " (current)" : ""
                  ]
                },
                le
              );
            })
          ]
        }
      )
    ] })
  ] });
}
const Bu = 32;
function Bm({
  trackId: e,
  segments: t,
  minTime: n,
  pps: r,
  contentWidth: l
}) {
  const o = Hu[e % Hu.length], i = [...t.keys()].sort((s, u) => s - u);
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:mb-1", children: [
    /* @__PURE__ */ v.jsxs(
      "div",
      {
        className: "jcs:flex jcs:items-center jcs:pl-1.5 jcs:text-[9px] jcs:font-semibold jcs:h-5 jcs:border-l-[3px]",
        style: { borderLeftColor: o, color: o },
        children: [
          "Track ",
          e + 1
        ]
      }
    ),
    i.length === 0 ? /* @__PURE__ */ v.jsx(
      "div",
      {
        className: "jcs:border-b jcs:border-border/50 jcs:mb-1",
        style: { height: Bu, width: l }
      }
    ) : i.map((s) => {
      const u = t.get(s), a = El[s % El.length];
      return /* @__PURE__ */ v.jsxs(
        "div",
        {
          className: "jcs:relative jcs:mb-1",
          style: { height: Bu, width: l },
          children: [
            /* @__PURE__ */ v.jsx(
              "div",
              {
                className: "jcs:absolute jcs:-left-0.5 jcs:top-0 jcs:text-[8px] jcs:opacity-60",
                style: { color: a },
                children: s >= 0 ? `seg ${s}` : "ungrouped"
              }
            ),
            u.words.map((p, h) => /* @__PURE__ */ v.jsx(
              Hm,
              {
                word: p,
                index: u.indices[h],
                minTime: n,
                pps: r,
                segmentIdx: s
              },
              u.indices[h]
            ))
          ]
        },
        s
      );
    })
  ] });
}
function Vm({ time: e, minTime: t, pps: n, height: r }) {
  const l = (e - t) * n;
  return l < 0 ? null : /* @__PURE__ */ v.jsxs(
    "div",
    {
      className: "jcs:absolute jcs:top-0 jcs:pointer-events-none jcs:z-10",
      style: { left: l, height: r },
      children: [
        /* @__PURE__ */ v.jsx(
          "div",
          {
            className: "jcs:absolute",
            style: {
              left: -5,
              top: 0,
              width: 0,
              height: 0,
              borderLeft: "5px solid transparent",
              borderRight: "5px solid transparent",
              borderTop: "7px solid #e53935",
              filter: "drop-shadow(0 0 2px rgba(229,57,53,0.6))"
            }
          }
        ),
        /* @__PURE__ */ v.jsx(
          "div",
          {
            className: "jcs:absolute",
            style: {
              left: -0.5,
              top: 7,
              width: 1,
              height: r - 7,
              background: "#e53935",
              boxShadow: "0 0 4px rgba(229,57,53,0.4)"
            }
          }
        )
      ]
    }
  );
}
const Qm = 8, Km = 28, Xm = 20, Ym = 32;
function Gm() {
  const e = T((g) => g.editedTimings), t = T((g) => g.zoomLevel), n = T((g) => g.scrollLeft), r = T((g) => g.setScrollLeft), l = T((g) => g.setZoomLevel), o = T((g) => g.trackCount), i = T((g) => g.addTrack), s = T((g) => g.resetEdits), u = T((g) => g.previewTime), a = T((g) => g.selectedWordIndex), p = T((g) => g.editingWordIndex), h = T((g) => g.deleteWord), m = T((g) => g.addWord), x = R.useRef(null), { minTime: _, maxTime: k, basePps: I, pps: f, contentWidth: c, trackMap: d } = R.useMemo(() => {
    if (e.length === 0)
      return {
        minTime: 0,
        maxTime: 0,
        totalDuration: 0,
        basePps: 80,
        pps: 80,
        contentWidth: 380,
        trackMap: /* @__PURE__ */ new Map()
      };
    const g = Math.min(...e.map((V) => V.start)), C = Math.max(...e.map((V) => V.end)), z = C - g, M = Math.max(80, 380 / (z || 1)), U = M * t, ce = Math.max(380, U * (z || 1)), $ = /* @__PURE__ */ new Map();
    e.forEach((V, Fe) => {
      const fe = V.track || 0, de = V.segment_idx ?? -1;
      $.has(fe) || $.set(fe, /* @__PURE__ */ new Map());
      const E = $.get(fe);
      E.has(de) || E.set(de, { words: [], indices: [] });
      const P = E.get(de);
      P.words.push(V), P.indices.push(Fe);
    });
    for (let V = 0; V < o; V++)
      $.has(V) || $.set(V, /* @__PURE__ */ new Map());
    return {
      minTime: g,
      maxTime: C,
      totalDuration: z,
      basePps: M,
      pps: U,
      contentWidth: ce,
      trackMap: $
    };
  }, [e, t, o]), y = R.useMemo(
    () => [...d.keys()].sort((g, C) => g - C),
    [d]
  ), S = R.useMemo(() => {
    let g = Km;
    for (const C of y) {
      const z = d.get(C).size;
      g += Xm, g += Math.max(1, z) * (Ym + 4);
    }
    return g + 40;
  }, [y, d]);
  R.useEffect(() => {
    const g = (C) => {
      (C.key === "Delete" || C.key === "Backspace") && a !== null && p === null && (C.preventDefault(), h(a));
    };
    return document.addEventListener("keydown", g), () => document.removeEventListener("keydown", g);
  }, [a, p, h]), R.useEffect(() => {
    const g = x.current;
    if (!g) return;
    const C = (z) => {
      if (!z.ctrlKey && !z.metaKey) return;
      z.preventDefault(), z.stopPropagation();
      const M = g.getBoundingClientRect(), ce = (z.clientX - M.left + g.scrollLeft) / f;
      let $;
      z.deltaY < 0 ? $ = Math.min(10, t * 1.1) : $ = Math.max(0.5, t / 1.1);
      const V = I * $, Fe = Math.max(
        0,
        ce * V - (z.clientX - M.left)
      );
      l($), r(Fe);
    };
    return g.addEventListener("wheel", C, { passive: !1 }), () => g.removeEventListener("wheel", C);
  }, [f, I, t, l, r]), R.useEffect(() => {
    x.current && (x.current.scrollLeft = n);
  }, [n]);
  const w = () => {
    x.current && r(x.current.scrollLeft);
  };
  return e.length === 0 ? /* @__PURE__ */ v.jsx("div", { className: "jcs:p-3 jcs:text-text-dim jcs:text-center", children: "Queue the workflow to populate the timeline." }) : /* @__PURE__ */ v.jsxs("div", { className: "jcs:border-b jcs:border-border", children: [
    /* @__PURE__ */ v.jsx(Dm, {}),
    /* @__PURE__ */ v.jsx(
      "div",
      {
        className: "jcs:overflow-x-auto jcs:overflow-y-hidden jcs:w-full jcs:relative",
        ref: x,
        onScroll: w,
        style: { padding: Qm },
        children: /* @__PURE__ */ v.jsxs(
          "div",
          {
            className: "jcs:relative",
            style: { width: c, minHeight: 100 },
            children: [
              /* @__PURE__ */ v.jsx(
                Vm,
                {
                  time: u,
                  minTime: _,
                  pps: f,
                  height: S
                }
              ),
              /* @__PURE__ */ v.jsx(Am, { minTime: _, maxTime: k, pps: f }),
              y.map((g) => /* @__PURE__ */ v.jsx(
                Bm,
                {
                  trackId: g,
                  segments: d.get(g),
                  minTime: _,
                  pps: f,
                  contentWidth: c
                },
                g
              )),
              /* @__PURE__ */ v.jsx(
                "button",
                {
                  className: "jcs:mt-1 jcs:text-[10px]",
                  onClick: i,
                  children: "+ Add Track"
                }
              )
            ]
          }
        )
      }
    ),
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:px-2 jcs:py-1 jcs:flex jcs:gap-2 jcs:items-center jcs:border-t jcs:border-border", children: [
      /* @__PURE__ */ v.jsx("button", { className: "jcs:text-[10px]", onClick: () => m(u), children: "+ Add Word" }),
      /* @__PURE__ */ v.jsx("div", { className: "jcs:flex-1" }),
      /* @__PURE__ */ v.jsx("button", { className: "jcs:text-[10px]", onClick: s, children: "Reset Edits" })
    ] })
  ] });
}
function Jm() {
  const e = T((u) => u.selectedWordIndex), t = T((u) => u.editedTimings), n = T((u) => u.setWordOverride), r = T((u) => u.clearWordOverrides), l = T((u) => u.selectWord);
  if (e === null || !t[e])
    return null;
  const o = t[e], i = e, s = "jcs:text-text-muted jcs:text-[9px] jcs:whitespace-nowrap";
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs:px-2 jcs:py-1.5 jcs:border-t jcs:border-border jcs:bg-panel", children: [
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2 jcs:flex-wrap", children: [
      /* @__PURE__ */ v.jsxs("span", { className: "jcs:text-[11px] jcs:font-semibold jcs:text-text jcs:whitespace-nowrap", children: [
        '"',
        o.word,
        '"',
        /* @__PURE__ */ v.jsxs("span", { className: "jcs:font-normal jcs:text-text-muted jcs:ml-1 jcs:text-[9px]", children: [
          "(",
          o.start.toFixed(3),
          "–",
          o.end.toFixed(3),
          "s)"
        ] })
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Text:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "text",
            className: "jcs:text-[10px] jcs:w-[72px]",
            value: o.override_text || "",
            placeholder: o.word,
            onChange: (u) => n(i, "override_text", u.target.value || void 0)
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Fill:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: o.override_color || "#FFFFFF",
            onChange: (u) => n(
              i,
              "override_color",
              u.target.value === "#ffffff" || u.target.value === "#FFFFFF" ? void 0 : u.target.value
            ),
            className: "jcs:w-5 jcs:h-5"
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Scale:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "number",
            className: "jcs:text-[10px] jcs:w-[40px]",
            value: o.override_scale ?? 1,
            min: 0.5,
            max: 3,
            step: 0.1,
            onChange: (u) => {
              const a = parseFloat(u.target.value);
              n(i, "override_scale", a === 1 ? void 0 : a);
            }
          }
        )
      ] })
    ] }),
    /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-2 jcs:mt-1 jcs:flex-wrap", children: [
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Highlight:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: o.override_highlight || "#00FFFF",
            onChange: (u) => n(
              i,
              "override_highlight",
              u.target.value === "#00ffff" || u.target.value === "#00FFFF" ? void 0 : u.target.value
            ),
            className: "jcs:w-5 jcs:h-5"
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Stroke:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "color",
            value: o.override_stroke_color || "#000000",
            onChange: (u) => n(
              i,
              "override_stroke_color",
              u.target.value === "#000000" ? void 0 : u.target.value
            ),
            className: "jcs:w-5 jcs:h-5"
          }
        ),
        /* @__PURE__ */ v.jsx("span", { className: "jcs:text-text-dim jcs:text-[9px]", children: "/" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "number",
            className: "jcs:text-[10px] jcs:w-[36px]",
            value: o.override_stroke_width ?? "",
            min: 0,
            max: 30,
            step: 1,
            placeholder: "—",
            onChange: (u) => n(i, "override_stroke_width", u.target.value === "" ? void 0 : parseFloat(u.target.value))
          }
        )
      ] }),
      /* @__PURE__ */ v.jsxs("div", { className: "jcs:flex jcs:items-center jcs:gap-1", children: [
        /* @__PURE__ */ v.jsx("span", { className: s, children: "Hidden:" }),
        /* @__PURE__ */ v.jsx(
          "input",
          {
            type: "checkbox",
            checked: o.override_hidden || !1,
            onChange: (u) => n(i, "override_hidden", u.target.checked || void 0)
          }
        )
      ] }),
      /* @__PURE__ */ v.jsx("div", { className: "jcs:flex-1" }),
      /* @__PURE__ */ v.jsx("button", { className: "jcs:text-[10px]", onClick: () => r(i), children: "Clear" }),
      /* @__PURE__ */ v.jsx("button", { className: "jcs:text-[10px]", onClick: () => l(null), children: "Deselect" })
    ] })
  ] });
}
function Zm() {
  const e = T((n) => n.selectedWordIndex), t = T((n) => n.styleExpanded);
  return /* @__PURE__ */ v.jsxs("div", { className: "jcs-root", children: [
    /* @__PURE__ */ v.jsx(zm, {}),
    /* @__PURE__ */ v.jsx(Lm, {}),
    t && /* @__PURE__ */ v.jsx(Om, {}),
    /* @__PURE__ */ v.jsx(Gm, {}),
    e !== null && /* @__PURE__ */ v.jsx(Jm, {})
  ] });
}
var yf = Symbol.for("immer-nothing"), Vu = Symbol.for("immer-draftable"), Me = Symbol.for("immer-state");
function Qe(e, ...t) {
  throw new Error(
    `[Immer] minified error nr: ${e}. Full error at: https://bit.ly/3cXEKWf`
  );
}
var gr = Object.getPrototypeOf;
function Nn(e) {
  return !!e && !!e[Me];
}
function Yt(e) {
  var t;
  return e ? xf(e) || Array.isArray(e) || !!e[Vu] || !!((t = e.constructor) != null && t[Vu]) || Cr(e) || Gl(e) : !1;
}
var qm = Object.prototype.constructor.toString(), Qu = /* @__PURE__ */ new WeakMap();
function xf(e) {
  if (!e || typeof e != "object")
    return !1;
  const t = Object.getPrototypeOf(e);
  if (t === null || t === Object.prototype)
    return !0;
  const n = Object.hasOwnProperty.call(t, "constructor") && t.constructor;
  if (n === Object)
    return !0;
  if (typeof n != "function")
    return !1;
  let r = Qu.get(n);
  return r === void 0 && (r = Function.toString.call(n), Qu.set(n, r)), r === qm;
}
function Fl(e, t, n = !0) {
  Yl(e) === 0 ? (n ? Reflect.ownKeys(e) : Object.keys(e)).forEach((l) => {
    t(l, e[l], e);
  }) : e.forEach((r, l) => t(l, r, e));
}
function Yl(e) {
  const t = e[Me];
  return t ? t.type_ : Array.isArray(e) ? 1 : Cr(e) ? 2 : Gl(e) ? 3 : 0;
}
function Si(e, t) {
  return Yl(e) === 2 ? e.has(t) : Object.prototype.hasOwnProperty.call(e, t);
}
function wf(e, t, n) {
  const r = Yl(e);
  r === 2 ? e.set(t, n) : r === 3 ? e.add(n) : e[t] = n;
}
function bm(e, t) {
  return e === t ? e !== 0 || 1 / e === 1 / t : e !== e && t !== t;
}
function Cr(e) {
  return e instanceof Map;
}
function Gl(e) {
  return e instanceof Set;
}
function Dt(e) {
  return e.copy_ || e.base_;
}
function Ci(e, t) {
  if (Cr(e))
    return new Map(e);
  if (Gl(e))
    return new Set(e);
  if (Array.isArray(e))
    return Array.prototype.slice.call(e);
  const n = xf(e);
  if (t === !0 || t === "class_only" && !n) {
    const r = Object.getOwnPropertyDescriptors(e);
    delete r[Me];
    let l = Reflect.ownKeys(r);
    for (let o = 0; o < l.length; o++) {
      const i = l[o], s = r[i];
      s.writable === !1 && (s.writable = !0, s.configurable = !0), (s.get || s.set) && (r[i] = {
        configurable: !0,
        writable: !0,
        // could live with !!desc.set as well here...
        enumerable: s.enumerable,
        value: e[i]
      });
    }
    return Object.create(gr(e), r);
  } else {
    const r = gr(e);
    if (r !== null && n)
      return { ...e };
    const l = Object.create(r);
    return Object.assign(l, e);
  }
}
function Fs(e, t = !1) {
  return Jl(e) || Nn(e) || !Yt(e) || (Yl(e) > 1 && Object.defineProperties(e, {
    set: Br,
    add: Br,
    clear: Br,
    delete: Br
  }), Object.freeze(e), t && Object.values(e).forEach((n) => Fs(n, !0))), e;
}
function eh() {
  Qe(2);
}
var Br = {
  value: eh
};
function Jl(e) {
  return e === null || typeof e != "object" ? !0 : Object.isFrozen(e);
}
var th = {};
function Gt(e) {
  const t = th[e];
  return t || Qe(0, e), t;
}
var yr;
function _f() {
  return yr;
}
function nh(e, t) {
  return {
    drafts_: [],
    parent_: e,
    immer_: t,
    // Whenever the modified draft contains a draft from another scope, we
    // need to prevent auto-freezing so the unowned draft can be finalized.
    canAutoFreeze_: !0,
    unfinalizedDrafts_: 0
  };
}
function Ku(e, t) {
  t && (Gt("Patches"), e.patches_ = [], e.inversePatches_ = [], e.patchListener_ = t);
}
function Ei(e) {
  Fi(e), e.drafts_.forEach(rh), e.drafts_ = null;
}
function Fi(e) {
  e === yr && (yr = e.parent_);
}
function Xu(e) {
  return yr = nh(yr, e);
}
function rh(e) {
  const t = e[Me];
  t.type_ === 0 || t.type_ === 1 ? t.revoke_() : t.revoked_ = !0;
}
function Yu(e, t) {
  t.unfinalizedDrafts_ = t.drafts_.length;
  const n = t.drafts_[0];
  return e !== void 0 && e !== n ? (n[Me].modified_ && (Ei(t), Qe(4)), Yt(e) && (e = Nl(t, e), t.parent_ || Tl(t, e)), t.patches_ && Gt("Patches").generateReplacementPatches_(
    n[Me].base_,
    e,
    t.patches_,
    t.inversePatches_
  )) : e = Nl(t, n, []), Ei(t), t.patches_ && t.patchListener_(t.patches_, t.inversePatches_), e !== yf ? e : void 0;
}
function Nl(e, t, n) {
  if (Jl(t))
    return t;
  const r = e.immer_.shouldUseStrictIteration(), l = t[Me];
  if (!l)
    return Fl(
      t,
      (o, i) => Gu(e, l, t, o, i, n),
      r
    ), t;
  if (l.scope_ !== e)
    return t;
  if (!l.modified_)
    return Tl(e, l.base_, !0), l.base_;
  if (!l.finalized_) {
    l.finalized_ = !0, l.scope_.unfinalizedDrafts_--;
    const o = l.copy_;
    let i = o, s = !1;
    l.type_ === 3 && (i = new Set(o), o.clear(), s = !0), Fl(
      i,
      (u, a) => Gu(
        e,
        l,
        o,
        u,
        a,
        n,
        s
      ),
      r
    ), Tl(e, o, !1), n && e.patches_ && Gt("Patches").generatePatches_(
      l,
      n,
      e.patches_,
      e.inversePatches_
    );
  }
  return l.copy_;
}
function Gu(e, t, n, r, l, o, i) {
  if (l == null || typeof l != "object" && !i)
    return;
  const s = Jl(l);
  if (!(s && !i)) {
    if (Nn(l)) {
      const u = o && t && t.type_ !== 3 && // Set objects are atomic since they have no keys.
      !Si(t.assigned_, r) ? o.concat(r) : void 0, a = Nl(e, l, u);
      if (wf(n, r, a), Nn(a))
        e.canAutoFreeze_ = !1;
      else
        return;
    } else i && n.add(l);
    if (Yt(l) && !s) {
      if (!e.immer_.autoFreeze_ && e.unfinalizedDrafts_ < 1 || t && t.base_ && t.base_[r] === l && s)
        return;
      Nl(e, l), (!t || !t.scope_.parent_) && typeof r != "symbol" && (Cr(n) ? n.has(r) : Object.prototype.propertyIsEnumerable.call(n, r)) && Tl(e, l);
    }
  }
}
function Tl(e, t, n = !1) {
  !e.parent_ && e.immer_.autoFreeze_ && e.canAutoFreeze_ && Fs(t, n);
}
function lh(e, t) {
  const n = Array.isArray(e), r = {
    type_: n ? 1 : 0,
    // Track which produce call this is associated with.
    scope_: t ? t.scope_ : _f(),
    // True for both shallow and deep changes.
    modified_: !1,
    // Used during finalization.
    finalized_: !1,
    // Track which properties have been assigned (true) or deleted (false).
    assigned_: {},
    // The parent draft state.
    parent_: t,
    // The base state.
    base_: e,
    // The base proxy.
    draft_: null,
    // set below
    // The base copy with any updated values.
    copy_: null,
    // Called by the `produce` function.
    revoke_: null,
    isManual_: !1
  };
  let l = r, o = Ns;
  n && (l = [r], o = xr);
  const { revoke: i, proxy: s } = Proxy.revocable(l, o);
  return r.draft_ = s, r.revoke_ = i, s;
}
var Ns = {
  get(e, t) {
    if (t === Me)
      return e;
    const n = Dt(e);
    if (!Si(n, t))
      return oh(e, n, t);
    const r = n[t];
    return e.finalized_ || !Yt(r) ? r : r === Co(e.base_, t) ? (Eo(e), e.copy_[t] = Ti(r, e)) : r;
  },
  has(e, t) {
    return t in Dt(e);
  },
  ownKeys(e) {
    return Reflect.ownKeys(Dt(e));
  },
  set(e, t, n) {
    const r = jf(Dt(e), t);
    if (r != null && r.set)
      return r.set.call(e.draft_, n), !0;
    if (!e.modified_) {
      const l = Co(Dt(e), t), o = l == null ? void 0 : l[Me];
      if (o && o.base_ === n)
        return e.copy_[t] = n, e.assigned_[t] = !1, !0;
      if (bm(n, l) && (n !== void 0 || Si(e.base_, t)))
        return !0;
      Eo(e), Ni(e);
    }
    return e.copy_[t] === n && // special case: handle new props with value 'undefined'
    (n !== void 0 || t in e.copy_) || // special case: NaN
    Number.isNaN(n) && Number.isNaN(e.copy_[t]) || (e.copy_[t] = n, e.assigned_[t] = !0), !0;
  },
  deleteProperty(e, t) {
    return Co(e.base_, t) !== void 0 || t in e.base_ ? (e.assigned_[t] = !1, Eo(e), Ni(e)) : delete e.assigned_[t], e.copy_ && delete e.copy_[t], !0;
  },
  // Note: We never coerce `desc.value` into an Immer draft, because we can't make
  // the same guarantee in ES5 mode.
  getOwnPropertyDescriptor(e, t) {
    const n = Dt(e), r = Reflect.getOwnPropertyDescriptor(n, t);
    return r && {
      writable: !0,
      configurable: e.type_ !== 1 || t !== "length",
      enumerable: r.enumerable,
      value: n[t]
    };
  },
  defineProperty() {
    Qe(11);
  },
  getPrototypeOf(e) {
    return gr(e.base_);
  },
  setPrototypeOf() {
    Qe(12);
  }
}, xr = {};
Fl(Ns, (e, t) => {
  xr[e] = function() {
    return arguments[0] = arguments[0][0], t.apply(this, arguments);
  };
});
xr.deleteProperty = function(e, t) {
  return xr.set.call(this, e, t, void 0);
};
xr.set = function(e, t, n) {
  return Ns.set.call(this, e[0], t, n, e[0]);
};
function Co(e, t) {
  const n = e[Me];
  return (n ? Dt(n) : e)[t];
}
function oh(e, t, n) {
  var l;
  const r = jf(t, n);
  return r ? "value" in r ? r.value : (
    // This is a very special case, if the prop is a getter defined by the
    // prototype, we should invoke it with the draft as context!
    (l = r.get) == null ? void 0 : l.call(e.draft_)
  ) : void 0;
}
function jf(e, t) {
  if (!(t in e))
    return;
  let n = gr(e);
  for (; n; ) {
    const r = Object.getOwnPropertyDescriptor(n, t);
    if (r)
      return r;
    n = gr(n);
  }
}
function Ni(e) {
  e.modified_ || (e.modified_ = !0, e.parent_ && Ni(e.parent_));
}
function Eo(e) {
  e.copy_ || (e.copy_ = Ci(
    e.base_,
    e.scope_.immer_.useStrictShallowCopy_
  ));
}
var ih = class {
  constructor(e) {
    this.autoFreeze_ = !0, this.useStrictShallowCopy_ = !1, this.useStrictIteration_ = !0, this.produce = (t, n, r) => {
      if (typeof t == "function" && typeof n != "function") {
        const o = n;
        n = t;
        const i = this;
        return function(u = o, ...a) {
          return i.produce(u, (p) => n.call(this, p, ...a));
        };
      }
      typeof n != "function" && Qe(6), r !== void 0 && typeof r != "function" && Qe(7);
      let l;
      if (Yt(t)) {
        const o = Xu(this), i = Ti(t, void 0);
        let s = !0;
        try {
          l = n(i), s = !1;
        } finally {
          s ? Ei(o) : Fi(o);
        }
        return Ku(o, r), Yu(l, o);
      } else if (!t || typeof t != "object") {
        if (l = n(t), l === void 0 && (l = t), l === yf && (l = void 0), this.autoFreeze_ && Fs(l, !0), r) {
          const o = [], i = [];
          Gt("Patches").generateReplacementPatches_(t, l, o, i), r(o, i);
        }
        return l;
      } else
        Qe(1, t);
    }, this.produceWithPatches = (t, n) => {
      if (typeof t == "function")
        return (i, ...s) => this.produceWithPatches(i, (u) => t(u, ...s));
      let r, l;
      return [this.produce(t, n, (i, s) => {
        r = i, l = s;
      }), r, l];
    }, typeof (e == null ? void 0 : e.autoFreeze) == "boolean" && this.setAutoFreeze(e.autoFreeze), typeof (e == null ? void 0 : e.useStrictShallowCopy) == "boolean" && this.setUseStrictShallowCopy(e.useStrictShallowCopy), typeof (e == null ? void 0 : e.useStrictIteration) == "boolean" && this.setUseStrictIteration(e.useStrictIteration);
  }
  createDraft(e) {
    Yt(e) || Qe(8), Nn(e) && (e = sh(e));
    const t = Xu(this), n = Ti(e, void 0);
    return n[Me].isManual_ = !0, Fi(t), n;
  }
  finishDraft(e, t) {
    const n = e && e[Me];
    (!n || !n.isManual_) && Qe(9);
    const { scope_: r } = n;
    return Ku(r, t), Yu(void 0, r);
  }
  /**
   * Pass true to automatically freeze all copies created by Immer.
   *
   * By default, auto-freezing is enabled.
   */
  setAutoFreeze(e) {
    this.autoFreeze_ = e;
  }
  /**
   * Pass true to enable strict shallow copy.
   *
   * By default, immer does not copy the object descriptors such as getter, setter and non-enumrable properties.
   */
  setUseStrictShallowCopy(e) {
    this.useStrictShallowCopy_ = e;
  }
  /**
   * Pass false to use faster iteration that skips non-enumerable properties
   * but still handles symbols for compatibility.
   *
   * By default, strict iteration is enabled (includes all own properties).
   */
  setUseStrictIteration(e) {
    this.useStrictIteration_ = e;
  }
  shouldUseStrictIteration() {
    return this.useStrictIteration_;
  }
  applyPatches(e, t) {
    let n;
    for (n = t.length - 1; n >= 0; n--) {
      const l = t[n];
      if (l.path.length === 0 && l.op === "replace") {
        e = l.value;
        break;
      }
    }
    n > -1 && (t = t.slice(n + 1));
    const r = Gt("Patches").applyPatches_;
    return Nn(e) ? r(e, t) : this.produce(
      e,
      (l) => r(l, t)
    );
  }
};
function Ti(e, t) {
  const n = Cr(e) ? Gt("MapSet").proxyMap_(e, t) : Gl(e) ? Gt("MapSet").proxySet_(e, t) : lh(e, t);
  return (t ? t.scope_ : _f()).drafts_.push(n), n;
}
function sh(e) {
  return Nn(e) || Qe(10, e), kf(e);
}
function kf(e) {
  if (!Yt(e) || Jl(e))
    return e;
  const t = e[Me];
  let n, r = !0;
  if (t) {
    if (!t.modified_)
      return t.base_;
    t.finalized_ = !0, n = Ci(e, t.scope_.immer_.useStrictShallowCopy_), r = t.scope_.immer_.shouldUseStrictIteration();
  } else
    n = Ci(e, !0);
  return Fl(
    n,
    (l, o) => {
      wf(n, l, kf(o));
    },
    r
  ), t && (t.finalized_ = !1), n;
}
var uh = new ih(), ah = uh.produce;
const ch = (e) => (t, n, r) => (r.setState = (l, o, ...i) => {
  const s = typeof l == "function" ? ah(l) : l;
  return t(s, o, ...i);
}, e(r.setState, n, r)), fh = ch, dh = () => km()(
  fh((e, t) => ({
    // Initial state
    originalTimings: [],
    editedTimings: [],
    style: { ...ji },
    presetName: "Custom",
    fps: 30,
    frameWidth: 1920,
    frameHeight: 1080,
    frameCount: 0,
    previewFrameBase64: "",
    fontList: [],
    frameAtlas: null,
    duration: 0,
    audioDataUrl: "",
    selectedWordIndex: null,
    zoomLevel: 1,
    scrollLeft: 0,
    trackCount: 1,
    previewTime: 0,
    isPlaying: !1,
    styleExpanded: !1,
    editingWordIndex: null,
    editingWordText: "",
    initFromPython: (n) => e((r) => {
      if (r.originalTimings = n.originalTimings, r.editedTimings.length === 0 && (r.editedTimings = n.editedTimings.length > 0 ? n.editedTimings : n.originalTimings.map((o) => ({ ...o }))), r.style = n.style, r.presetName = n.presetName, r.fps = n.fps, r.frameWidth = n.frameWidth, r.frameHeight = n.frameHeight, r.frameCount = n.frameCount, r.previewFrameBase64 = n.previewFrameBase64, r.fontList = n.fontList, n.frameAtlas && (r.frameAtlas = n.frameAtlas), r.audioDataUrl = n.audioDataUrl || "", n.frameCount > 0 && n.fps > 0)
        r.duration = n.frameCount / n.fps;
      else {
        const o = r.editedTimings;
        r.duration = o.length > 0 ? Math.max(...o.map((i) => i.end)) : 0;
      }
      const l = Math.max(
        0,
        ...r.editedTimings.map((o) => o.track || 0)
      );
      l + 1 > r.trackCount && (r.trackCount = l + 1);
    }),
    moveWord: (n, r, l) => e((o) => {
      o.editedTimings[n] && (o.editedTimings[n].start = Math.round(r * 1e3) / 1e3, o.editedTimings[n].end = Math.round(l * 1e3) / 1e3);
    }),
    resizeWord: (n, r) => e((l) => {
      l.editedTimings[n] && (l.editedTimings[n].end = Math.round(r * 1e3) / 1e3);
    }),
    selectWord: (n) => e((r) => {
      r.selectedWordIndex = n;
    }),
    moveWordToTrack: (n, r) => e((l) => {
      l.editedTimings[n] && (l.editedTimings[n].track = r);
    }),
    addTrack: () => e((n) => {
      n.trackCount++;
    }),
    setPreset: (n) => e((r) => {
      r.presetName = n;
    }),
    updateStyle: (n) => e((r) => {
      Object.assign(r.style, n), r.presetName = "Custom";
    }),
    setWordOverride: (n, r, l) => e((o) => {
      o.editedTimings[n] && (o.editedTimings[n][r] = l);
    }),
    clearWordOverrides: (n) => e((r) => {
      const l = r.editedTimings[n];
      l && (delete l.override_text, delete l.override_color, delete l.override_highlight, delete l.override_scale, delete l.override_font_size, delete l.override_stroke_width, delete l.override_stroke_color, delete l.override_hidden);
    }),
    addWord: (n) => e((r) => {
      const o = {
        word: "new",
        start: Math.round(n * 1e3) / 1e3,
        end: Math.round((n + 0.3) * 1e3) / 1e3,
        segment_idx: -1,
        track: 0
      };
      r.editedTimings.push(o);
      const i = r.editedTimings.length - 1;
      r.selectedWordIndex = i, r.editingWordIndex = i, r.editingWordText = "new";
    }),
    deleteWord: (n) => e((r) => {
      n >= 0 && n < r.editedTimings.length && (r.editedTimings.splice(n, 1), r.selectedWordIndex === n ? r.selectedWordIndex = null : r.selectedWordIndex !== null && r.selectedWordIndex > n && r.selectedWordIndex--, r.editingWordIndex === n ? (r.editingWordIndex = null, r.editingWordText = "") : r.editingWordIndex !== null && r.editingWordIndex > n && r.editingWordIndex--);
    }),
    duplicateWord: (n) => e((r) => {
      const l = r.editedTimings[n];
      if (!l) return;
      const o = l.end - l.start, i = {
        ...l,
        start: Math.round(l.end * 1e3) / 1e3,
        end: Math.round((l.end + o) * 1e3) / 1e3
      };
      r.editedTimings.splice(n + 1, 0, i), r.selectedWordIndex = n + 1;
    }),
    resetEdits: () => e((n) => {
      n.editedTimings = n.originalTimings.map((r) => ({ ...r })), n.selectedWordIndex = null, n.trackCount = 1, n.presetName = "Custom", n.style = { ...ji };
    }),
    setZoomLevel: (n) => e((r) => {
      r.zoomLevel = Math.max(0.5, Math.min(10, n));
    }),
    setScrollLeft: (n) => e((r) => {
      r.scrollLeft = n;
    }),
    setPreviewTime: (n) => e((r) => {
      r.previewTime = n;
    }),
    togglePlay: () => e((n) => {
      n.isPlaying = !n.isPlaying;
    }),
    setIsPlaying: (n) => e((r) => {
      r.isPlaying = n;
    }),
    toggleStyleExpanded: () => e((n) => {
      n.styleExpanded = !n.styleExpanded;
    }),
    stepFrame: (n) => e((r) => {
      const l = 1 / (r.fps || 30), o = Math.max(0, Math.min(r.duration, r.previewTime + n * l));
      r.previewTime = Math.round(o * 1e3) / 1e3, r.isPlaying = !1;
    }),
    seekToTime: (n) => e((r) => {
      r.previewTime = Math.max(0, Math.min(r.duration, n)), r.isPlaying = !1;
    }),
    startEditingWord: (n) => e((r) => {
      r.editedTimings[n] && (r.editingWordIndex = n, r.editingWordText = r.editedTimings[n].override_text || r.editedTimings[n].word);
    }),
    commitEditingWord: () => e((n) => {
      if (n.editingWordIndex !== null && n.editedTimings[n.editingWordIndex]) {
        const r = n.editedTimings[n.editingWordIndex].word, l = n.editingWordText.trim();
        l && l !== r ? n.editedTimings[n.editingWordIndex].override_text = l : l === r && delete n.editedTimings[n.editingWordIndex].override_text;
      }
      n.editingWordIndex = null, n.editingWordText = "";
    }),
    cancelEditingWord: () => e((n) => {
      n.editingWordIndex = null, n.editingWordText = "";
    }),
    setEditingWordText: (n) => e((r) => {
      r.editingWordText = n;
    }),
    getEditedTimingsJSON: () => {
      const { editedTimings: n } = t(), r = n.map((l) => {
        const o = {
          word: l.word,
          start: Math.round(l.start * 1e3) / 1e3,
          end: Math.round(l.end * 1e3) / 1e3,
          segment_idx: l.segment_idx ?? -1,
          track: l.track || 0
        };
        return l.override_text && (o.override_text = l.override_text), l.override_color && (o.override_color = l.override_color), l.override_highlight && (o.override_highlight = l.override_highlight), l.override_scale && l.override_scale !== 1 && (o.override_scale = l.override_scale), l.override_font_size && (o.override_font_size = l.override_font_size), l.override_stroke_width != null && (o.override_stroke_width = l.override_stroke_width), l.override_stroke_color && (o.override_stroke_color = l.override_stroke_color), l.override_hidden && (o.override_hidden = l.override_hidden), o;
      });
      return JSON.stringify(r);
    },
    getStyleConfigJSON: () => {
      const { style: n, presetName: r } = t(), l = {
        ...n,
        _preset: r
      };
      return JSON.stringify(l);
    }
  }))
), Pl = /* @__PURE__ */ new Map(), Ju = /* @__PURE__ */ new Map();
let Pi = null, zi = null, Fo = null, Mi = null;
function ph(e) {
  let t = Pl.get(e);
  return t || (t = dh(), Pl.set(e, t)), t;
}
function mh(e, t) {
  Pi = e, zi = t;
}
function hh(e, t) {
  if (Ju.has(t)) return;
  const n = ph(t);
  Mi = t, e.innerHTML = "";
  const r = To.createRoot(e);
  Ju.set(t, r), n.subscribe(() => {
    gh(t);
  }), r.render(
    No.createElement(
      gf.Provider,
      { value: n },
      No.createElement(Zm)
    )
  );
}
function vh(e) {
  var n, r, l, o, i, s, u, a, p, h, m, x;
  if (!Mi) return;
  const t = Pl.get(Mi);
  if (t)
    try {
      const _ = (n = e.timings_json) == null ? void 0 : n[0], k = (r = e.edited_timings_json) == null ? void 0 : r[0], I = (l = e.style_json) == null ? void 0 : l[0], f = (o = e.style_presets) == null ? void 0 : o[0], c = ((i = e.fps) == null ? void 0 : i[0]) ?? 30, d = ((s = e.frame_width) == null ? void 0 : s[0]) ?? 1920, y = ((u = e.frame_height) == null ? void 0 : u[0]) ?? 1080, S = ((a = e.frame_count) == null ? void 0 : a[0]) ?? 0, w = ((p = e.preview_frame) == null ? void 0 : p[0]) ?? "", g = (h = e.font_list) == null ? void 0 : h[0], C = (m = e.frame_atlas) == null ? void 0 : m[0], z = ((x = e.audio_data) == null ? void 0 : x[0]) ?? "", M = _ ? JSON.parse(_) : [];
      let U = [];
      if (k && k.trim())
        try {
          U = JSON.parse(k);
        } catch {
          U = [];
        }
      const ce = I ? JSON.parse(I) : { ...ji };
      let $ = "Custom";
      if (f)
        try {
          const fe = JSON.parse(f);
          for (const [de, E] of Object.entries(fe))
            if (JSON.stringify(E) === JSON.stringify(ce)) {
              $ = de;
              break;
            }
        } catch {
        }
      const V = g ? JSON.parse(g) : [];
      let Fe = null;
      if (C && C.trim())
        try {
          Fe = JSON.parse(C);
        } catch {
        }
      t.getState().initFromPython({
        originalTimings: M,
        editedTimings: U,
        style: ce,
        presetName: $,
        fps: c,
        frameWidth: d,
        frameHeight: y,
        frameCount: S,
        previewFrameBase64: w,
        fontList: V,
        frameAtlas: Fe,
        audioDataUrl: z ? `data:audio/wav;base64,${z}` : ""
      });
    } catch (_) {
      console.error("[Ordo] Caption Studio: failed to parse Python output", _);
    }
}
function gh(e) {
  Fo && clearTimeout(Fo), Fo = setTimeout(() => {
    yh(e);
  }, 300);
}
function yh(e) {
  const t = Pl.get(e);
  if (!t) return;
  const n = t.getState();
  Pi && (Pi.value = n.getEditedTimingsJSON()), zi && (zi.value = n.getStyleConfigJSON());
}
const xh = /* @__PURE__ */ Object.freeze(/* @__PURE__ */ Object.defineProperty({
  __proto__: null,
  mountCaptionStudio: hh,
  receiveFromPython: vh,
  setWidgetRefs: mh
}, Symbol.toStringTag, { value: "Module" }));
function wh(e, t, n) {
  const r = Math.floor(e * n), l = r * 2, o = new ArrayBuffer(44 + l), i = new DataView(o), s = (p, h) => {
    for (let m = 0; m < h.length; m++) i.setUint8(p + m, h.charCodeAt(m));
  };
  s(0, "RIFF"), i.setUint32(4, 36 + l, !0), s(8, "WAVE"), s(12, "fmt "), i.setUint32(16, 16, !0), i.setUint16(20, 1, !0), i.setUint16(22, 1, !0), i.setUint32(24, n, !0), i.setUint32(28, n * 2, !0), i.setUint16(32, 2, !0), i.setUint16(34, 16, !0), s(36, "data"), i.setUint32(40, l, !0);
  for (let p = 0; p < r; p++) {
    const h = p / n, m = Math.sin(2 * Math.PI * t * h) * 0.3, x = Math.max(-32768, Math.min(32767, Math.floor(m * 32767)));
    i.setInt16(44 + p * 2, x, !0);
  }
  const u = new Uint8Array(o);
  let a = "";
  for (let p = 0; p < u.length; p++)
    a += String.fromCharCode(u[p]);
  return btoa(a);
}
function _h(e, t, n) {
  const r = document.createElement("canvas");
  r.width = e, r.height = t;
  const l = r.getContext("2d"), o = l.createLinearGradient(0, 0, e, t);
  o.addColorStop(0, `hsl(${n}, 60%, 25%)`), o.addColorStop(0.5, `hsl(${(n + 60) % 360}, 50%, 35%)`), o.addColorStop(1, `hsl(${(n + 120) % 360}, 40%, 20%)`), l.fillStyle = o, l.fillRect(0, 0, e, t);
  const i = n / 360;
  return l.fillStyle = "rgba(255,255,255,0.15)", l.fillRect(0, t - 4, e * i, 4), r.toDataURL("image/jpeg", 0.6).split(",")[1];
}
typeof window < "u" && document.getElementById("root") && Promise.resolve().then(() => xh).then(({ mountCaptionStudio: e, receiveFromPython: t }) => {
  const n = document.getElementById("root");
  e(n, "dev-0");
  const r = [
    { word: "Caption", start: 0, end: 0.28, segment_idx: 0, track: 0 },
    { word: "Studio", start: 0.3, end: 0.58, segment_idx: 0, track: 0 },
    { word: "gives", start: 0.6, end: 0.82, segment_idx: 0, track: 0 },
    { word: "you", start: 0.84, end: 0.98, segment_idx: 1, track: 0 },
    { word: "complete", start: 1, end: 1.35, segment_idx: 1, track: 0 },
    { word: "control", start: 1.38, end: 1.72, segment_idx: 1, track: 0 },
    { word: "over", start: 1.75, end: 1.92, segment_idx: 2, track: 0 },
    { word: "every", start: 1.95, end: 2.18, segment_idx: 2, track: 0 },
    { word: "single", start: 2.2, end: 2.48, segment_idx: 2, track: 0 },
    { word: "word", start: 2.5, end: 2.72, segment_idx: 3, track: 0 },
    { word: "in", start: 2.74, end: 2.82, segment_idx: 3, track: 0 },
    { word: "your", start: 2.84, end: 2.98, segment_idx: 3, track: 0 },
    { word: "captions", start: 3, end: 3.42, segment_idx: 4, track: 0 },
    { word: "with", start: 3.45, end: 3.58, segment_idx: 4, track: 0 },
    { word: "real", start: 3.6, end: 3.82, segment_idx: 4, track: 0 },
    { word: "time", start: 3.85, end: 4.12, segment_idx: 5, track: 0 },
    { word: "preview", start: 4.15, end: 4.52, segment_idx: 5, track: 0 },
    { word: "and", start: 4.55, end: 4.65, segment_idx: 5, track: 0 },
    { word: "editing", start: 4.68, end: 5.1, segment_idx: 5, track: 0 }
  ], l = wh(5, 440, 22050), o = 480, i = 270, s = 150, u = 5, a = Math.ceil(s / u), p = [];
  for (let h = 0; h < a; h++) {
    const m = h / a * 360;
    p.push(_h(o, i, m));
  }
  t({
    timings_json: [JSON.stringify(r)],
    edited_timings_json: [""],
    style_json: [JSON.stringify({
      font_path: "C:/Windows/Fonts/impact.ttf",
      font_size: 120,
      fill_color: "#FFFFFF",
      highlight_colors: ["#00FFFF", "#00FF00", "#FFFF00", "#FF4444"],
      stroke_color: "#000000",
      stroke_width: 8,
      shadow_color: "#000000",
      shadow_opacity: 0.6,
      shadow_offset_x: 4,
      shadow_offset_y: 4,
      shadow_blur: 6,
      anim_duration: 0.15,
      y_position: 0.45,
      uppercase: !0
    })],
    style_presets: ["{}"],
    fps: [30],
    frame_width: [1920],
    frame_height: [1080],
    frame_count: [s],
    preview_frame: [""],
    font_list: [JSON.stringify([
      "C:/Windows/Fonts/arial.ttf",
      "C:/Windows/Fonts/arialbd.ttf",
      "C:/Windows/Fonts/georgia.ttf",
      "C:/Windows/Fonts/impact.ttf",
      "C:/Windows/Fonts/segoeui.ttf",
      "C:/Windows/Fonts/segoeuib.ttf"
    ])],
    frame_atlas: [JSON.stringify({
      frames: p,
      interval: u,
      fps: 30,
      width: o,
      height: i
    })],
    audio_data: [l]
  });
});
export {
  hh as mountCaptionStudio,
  vh as receiveFromPython,
  mh as setWidgetRefs
};
