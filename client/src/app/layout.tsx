import "./globals.css";
import { SessionProvider } from "next-auth/react";
import ClientShell from "./components/ClientShell";
import ConditionalShell from "./components/ConditionalShell";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {

  return (
    <html lang="en" className="h-full" suppressHydrationWarning>
      <head>
        <script src="/env-config.js" />
        {process.env.NODE_ENV === "development" && (
          <script
            dangerouslySetInnerHTML={{
              __html: `(function(){
  var css="#devtools-indicator{left:auto!important;right:16px!important;bottom:16px!important;}";
  function park(portal){
    var root=portal.shadowRoot;
    if(!root) return;
    if(!root.getElementById("aurora-indicator-pos")){
      var style=document.createElement("style");
      style.id="aurora-indicator-pos";
      style.textContent=css;
      root.appendChild(style);
      new MutationObserver(function(){park(portal);}).observe(root,{childList:true,subtree:true});
    }
    if(root.querySelector("#devtools-indicator")) portal.style.visibility="visible";
  }
  function scan(){
    document.querySelectorAll("nextjs-portal").forEach(park);
  }
  scan();
  new MutationObserver(scan).observe(document.documentElement,{childList:true,subtree:true});
  setTimeout(function(){
    document.querySelectorAll("nextjs-portal").forEach(function(portal){
      var root=portal.shadowRoot;
      if(!root||!root.querySelector("#devtools-indicator")) portal.style.visibility="visible";
    });
  }, 2000);
})();`,
            }}
          />
        )}
      </head>
      <body className="antialiased h-full">
        <SessionProvider refetchOnWindowFocus={true}>
          <ConditionalShell>{children}</ConditionalShell>
        </SessionProvider>
      </body>
    </html>
  );
}
